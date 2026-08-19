# Hephaestus Worker — 赫菲斯托斯之工坊
# 蒸馏 Worker — 自动处理 distill_queue，通过 LLM API 执行蒸馏

"""
职责：
- 轮询 distill_queue/ 中的待蒸馏任务（默认跟随 claude_data_dir）
- 调用 OpenAI 兼容 LLM API 完成蒸馏（主备链 failover）
- 通过唯一的同步引擎路径校验并提交 typed write receipt

设计原则：Mnemos 直接调用 LLM API 执行蒸馏，不委托宿主 Agent。
        品质可控：硬校验 → 入库/失败分流 → 复盘提醒。
"""

import json
import hashlib
import logging
import sqlite3
import threading
import time
from concurrent.futures import Future, TimeoutError as FutureTimeoutError
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Callable

from core.config import get_config
from core.ops.durable_io import secure_publish_immutable_text
from core.prometheus_fire import QueueDistillTask
from core.sync_framework.storage_backend import StorageError

# Constants extracted from magic numbers
HEPHAESTUS_WORKER__COMPLETED_NOTIFIED_MAX = 10000

logger = logging.getLogger(__name__)

WORKER_OPERATION_ERRORS = (
    ImportError,
    KeyError,
    OSError,
    RuntimeError,
    TypeError,
    ValueError,
    sqlite3.Error,
)
WORKER_STORAGE_ERRORS = WORKER_OPERATION_ERRORS + (StorageError,)


class DistillationWorkerCycleError(RuntimeError):
    """A control-plane/storage failure that must not look like an empty cycle."""

    def __init__(self, code: str) -> None:
        self.code = str(code)
        super().__init__(self.code)


def _normalize_messages(messages) -> List[Dict]:
    """Normalize every payload shape declared by the Amphora task contract."""
    if isinstance(messages, str):
        return [{"role": "user", "content": messages}]
    if isinstance(messages, dict):
        return [messages]
    if isinstance(messages, list):
        normalized = []
        for item in messages:
            if isinstance(item, dict):
                normalized.append(item)
            elif isinstance(item, str):
                normalized.append({"role": "user", "content": item})
        return normalized
    return []


class HephaestusWorker:
    """蒸馏 Worker — 火神工坊

    自动处理 distill_queue，将原始对话蒸馏为结构化知识。
    """

    # [P1-18] 已通知完成的 session_id 集合上限，防止内存泄漏
    _COMPLETED_NOTIFIED_MAX = HEPHAESTUS_WORKER__COMPLETED_NOTIFIED_MAX
    # [P1-018] 任务描述文件大小上限（10 MB），加载前检查避免 OOM
    _MAX_TASK_FILE_SIZE_BYTES = 10 * 1024 * 1024

    def __init__(
        self,
        queue_dir: Path | None = None,
        inbox_dir: Path | None = None,
        archive_dir: Path | None = None,
    ):
        self.config = get_config()
        self._queue_dir = queue_dir
        self._inbox_dir = inbox_dir
        self._archive_dir = archive_dir
        self._completed_notified: set = set()
        # [P0-7] 停止事件，用于优雅退出 watch_queue
        self._stop_event = threading.Event()
        self._late_futures_lock = threading.Lock()
        self._late_futures: dict[str, tuple[threading.Thread, Future]] = {}
        self._late_claims: dict[str, tuple[str, str]] = {}
        self._late_transition_failures: dict[
            str,
            tuple[str, str, Dict | None],
        ] = {}

    @property
    def queue_dir(self) -> Path:
        """Return the task-artifact directory rooted under database_dir."""
        if self._queue_dir:
            return self._queue_dir
        return get_config().database_dir / "distill_queue"

    @property
    def inbox_dir(self) -> Path:
        """Wiki Inbox 目录"""
        if self._inbox_dir:
            return self._inbox_dir
        return self.config.wiki_dir / "00-Inbox"

    @property
    def archive_dir(self) -> Path:
        "已处理队列文件归档目录"
        if self._archive_dir:
            return self._archive_dir
        return get_config().database_dir / "distill_archive"

    @property
    def backend(self):
        """StorageBackend 实例（懒加载）"""
        if not hasattr(self, "_backend"):
            from core.sync_framework.storage_backend import create_storage_backend

            self._backend = create_storage_backend(vault_path=self.inbox_dir.parent)
        return self._backend

    def _mark_l1_distilled(self, session_id: str):
        """[P102] 蒸馏成功后，将 L1 中对应 session 的记录标记为 status=distilled。"""
        if not session_id:
            return
        try:
            results = self.backend.list_by_tags([f"session={session_id}"], limit=100)
            if not results:
                return
            for res in results:
                try:
                    self.backend.update_tags(res.uid, add_tags=["status=distilled"])
                    logger.debug("[Hephaestus] 已标记 L1 为 distilled: %s", res.uid)
                except WORKER_STORAGE_ERRORS as exc:
                    logger.warning("[Hephaestus] 标记 L1 distilled 失败 %s: %s", res.uid, exc)
        except WORKER_STORAGE_ERRORS as exc:
            logger.warning("[Hephaestus] 查询 L1 session 失败 %s: %s", session_id, exc)

    def process_all(self, max_tasks: int | None = None) -> int:
        """处理队列中所有待蒸馏任务。

        仅从 amphora SQLite 队列读取任务并处理。

        Returns:
            处理的任务数量
        """
        # 确保数据库目录存在（amphora SQLite 队列以此为根基，不依赖文件任务目录）
        self.config.database_dir.mkdir(parents=True, exist_ok=True)

        # Trusted proposals are a persisted non-terminal state. Reconcile user
        # decisions before considering LLM pause/backoff so approval never stalls.
        maintenance_failures: list[str] = []
        maintenance_actions = (
            ("proposal", self.reconcile_proposal_tasks),
            ("success_terminal", self.reconcile_terminal_receipts),
            ("failed_terminal", self.reconcile_failed_terminal_receipts),
            ("timeout", self._recover_expired_delegations),
        )
        for owner, action in maintenance_actions:
            try:
                action()
            except DistillationWorkerCycleError as exc:
                maintenance_failures.append(f"{owner}:{exc.code}")
                logger.error(
                    "[Hephaestus] maintenance owner unavailable: %s",
                    owner,
                    exc_info=True,
                )
        if maintenance_failures:
            raise DistillationWorkerCycleError(
                "distillation_maintenance_unavailable:"
                + ",".join(maintenance_failures)
            )

        pause_checker: Callable[[], bool] | None = None
        # 检查蒸馏是否被暂停
        try:
            from core.hephaestus.distillation_engine import is_distillation_paused

            pause_checker = is_distillation_paused
            if pause_checker():
                logger.info("[Hephaestus] 蒸馏当前处于暂停状态，跳过本轮处理")
                return 0
        except (
            OSError,
            ValueError,
            TypeError,
            KeyError,
            ImportError,
            AttributeError,
            RuntimeError,
        ) as exc:
            raise DistillationWorkerCycleError(
                "distillation_pause_state_unavailable"
            ) from exc

        if max_tasks is None:
            max_tasks = int(self.config.get("distill.max_tasks_per_cycle", 5) or 5)
        if max_tasks <= 0:
            logger.info("[Hephaestus] 本轮 max_tasks<=0，跳过队列处理")
            return 0

        try:
            from core.kia import amphora
        except ImportError as exc:
            raise DistillationWorkerCycleError("amphora_import_unavailable") from exc

        try:
            pending_tasks = amphora.list_pending(include_future_retry=False)
        except WORKER_OPERATION_ERRORS as exc:
            raise DistillationWorkerCycleError("amphora_pending_scan_failed") from exc

        if not pending_tasks:
            return 0

        if len(pending_tasks) > max_tasks:
            logger.info(
                "[Hephaestus] 队列积压 %d 个任务，本轮限量处理 %d 个",
                len(pending_tasks),
                max_tasks,
            )

        processed = 0
        min_interval = float(self.config.get("distill.min_task_interval_seconds", 1.0) or 0.0)
        for i in range(max_tasks):
            try:
                try:
                    paused = pause_checker is not None and pause_checker()
                except WORKER_OPERATION_ERRORS as exc:
                    raise DistillationWorkerCycleError(
                        "distillation_pause_state_unavailable"
                    ) from exc
                if paused:
                    logger.info("[Hephaestus] 蒸馏进入暂停状态，停止本轮队列处理")
                    break
                try:
                    task = amphora.get_next()
                except WORKER_OPERATION_ERRORS as exc:
                    raise DistillationWorkerCycleError(
                        "amphora_task_claim_failed"
                    ) from exc
                if not task:
                    break
                if self.process_one_task(task):
                    processed += 1
            except DistillationWorkerCycleError:
                raise
            except WORKER_STORAGE_ERRORS as exc:
                raise DistillationWorkerCycleError(
                    "distillation_task_execution_failed"
                ) from exc
            # 任务间隔：避免积压恢复时一次性跑满 CPU/磁盘/宿主 Agent
            if min_interval > 0 and i < max_tasks - 1:
                time.sleep(min_interval)

        return processed

    def reconcile_proposal_tasks(self, limit: int = 100) -> int:
        """Advance proposal_pending tasks only after decisions and pages are durable."""
        try:
            from core.kia import amphora
            from core.pipeline_receipts import DistillationWriteReceipt
            from core.trust.config import load_trusted_push_config
            from core.trust.proposal_queue import ProposalQueue

            tasks = amphora.list_tasks(status="proposal_pending", limit=limit)
            if not tasks:
                return 0
            trusted_config = load_trusted_push_config(wiki_base=self.config.wiki_dir)
            queue = ProposalQueue(
                trusted_config.db_path,
                wiki_base=self.config.wiki_dir,
                config=trusted_config,
            )
        except (ImportError, OSError, ValueError, sqlite3.Error) as exc:
            raise DistillationWorkerCycleError(
                "trusted_proposal_scan_unavailable"
            ) from exc

        reconciled = 0
        for task in tasks:
            proposal_ids = [str(value) for value in task.get("proposal_ids", []) if value]
            existing_paths = tuple(
                dict.fromkeys(str(value) for value in task.get("written_paths", []) if value)
            )
            if not proposal_ids:
                receipt = DistillationWriteReceipt(
                    status="retryable_failed",
                    terminal_reason="proposal_pending_task_has_no_proposal_receipt",
                    expected_count=int(task.get("written_count") or 0) + 1,
                    written_count=int(task.get("written_count") or 0),
                    failed_count=1,
                )
            else:
                proposals = []
                try:
                    proposals = [queue.get(proposal_id) for proposal_id in proposal_ids]
                except KeyError:
                    receipt = DistillationWriteReceipt(
                        status="retryable_failed",
                        terminal_reason="trusted_proposal_receipt_missing",
                        proposal_ids=tuple(proposal_ids),
                        expected_count=len(proposal_ids),
                        failed_count=len(proposal_ids),
                    )
                except (OSError, ValueError, sqlite3.Error) as exc:
                    raise DistillationWorkerCycleError(
                        "trusted_proposal_store_unavailable"
                    ) from exc
                else:
                    statuses = [str(proposal.status) for proposal in proposals]
                    if any(
                        status not in {"committed", "rejected", "failed"} for status in statuses
                    ):
                        continue
                    consumer_receipts = tuple(
                        f"proposal:{proposal_id}:{status}"
                        for proposal_id, status in zip(proposal_ids, statuses)
                    )
                    if any(status == "failed" for status in statuses):
                        receipt = DistillationWriteReceipt(
                            status="retryable_failed",
                            terminal_reason="trusted_proposal_write_failed",
                            proposal_ids=tuple(proposal_ids),
                            expected_count=len(proposal_ids),
                            failed_count=sum(status == "failed" for status in statuses),
                            required_consumer_receipts=consumer_receipts,
                        )
                    elif all(status == "rejected" for status in statuses) and not existing_paths:
                        receipt = DistillationWriteReceipt(
                            status="intentional_skip",
                            terminal_reason="all_trusted_proposals_explicitly_rejected",
                            proposal_ids=tuple(proposal_ids),
                            expected_count=len(proposal_ids),
                            required_consumer_receipts=consumer_receipts,
                        )
                    else:
                        committed_paths = tuple(
                            dict.fromkeys(
                                (
                                    *existing_paths,
                                    *(
                                        str(proposal.candidate.target_path)
                                        for proposal in proposals
                                        if proposal.status == "committed"
                                    ),
                                )
                            )
                        )
                        missing_paths = [
                            path for path in committed_paths if not Path(path).exists()
                        ]
                        if missing_paths:
                            receipt = DistillationWriteReceipt(
                                status="retryable_failed",
                                terminal_reason="proposal_committed_without_target_page",
                                written_pages=committed_paths,
                                proposal_ids=tuple(proposal_ids),
                                expected_count=len(existing_paths) + len(proposal_ids),
                                written_count=len(committed_paths) - len(missing_paths),
                                failed_count=len(missing_paths),
                                required_consumer_receipts=consumer_receipts,
                            )
                        else:
                            receipt = DistillationWriteReceipt(
                                status="committed",
                                terminal_reason=(
                                    "trusted_proposals_committed_with_explicit_rejections"
                                    if any(status == "rejected" for status in statuses)
                                    else "all_trusted_proposals_committed"
                                ),
                                written_pages=committed_paths,
                                proposal_ids=tuple(proposal_ids),
                                expected_count=len(existing_paths) + len(proposal_ids),
                                written_count=len(committed_paths),
                                required_consumer_receipts=consumer_receipts,
                            )
            try:
                marked = amphora.mark_terminal(
                    str(task["task_id"]),
                    receipt,
                    expected_started_at=str(task.get("started_at") or ""),
                )
            except WORKER_OPERATION_ERRORS as exc:
                raise DistillationWorkerCycleError(
                    "trusted_proposal_transition_failed"
                ) from exc
            if marked:
                reconciled += 1
                if receipt.terminal:
                    try:
                        self.reconcile_terminal_receipts(
                            identifier=str(task["task_id"]),
                        )
                    except DistillationWorkerCycleError:
                        logger.error(
                            "[Hephaestus] terminal outbox replay deferred",
                            exc_info=True,
                        )
        return reconciled

    def _recover_expired_delegations(self, max_age_hours: int = 24):
        """检查已委托但超时的任务，恢复为待处理状态重新委托

        委托给 amphora 的 reset_timeouts() 实现。
        """
        try:
            from core.kia import amphora

            with self._late_futures_lock:
                pending_transitions = tuple(self._late_transition_failures.items())

            transition_error: DistillationWorkerCycleError | None = None
            for key, (identifier, error, task) in pending_transitions:
                try:
                    self._mark_amphora_failed(identifier, error, task=task)
                except DistillationWorkerCycleError as exc:
                    transition_error = exc
                    continue
                with self._late_futures_lock:
                    self._late_transition_failures.pop(key, None)
                    self._late_claims.pop(key, None)

            with self._late_futures_lock:
                excluded_claims = tuple(self._late_claims.values())
            recovered = amphora.reset_timeouts(
                timeout_minutes=max_age_hours * 60,
                excluded_claims=excluded_claims,
            )
            if recovered > 0:
                logger.info("超时任务恢复: %s 个任务已恢复为待处理", recovered)
            if transition_error is not None:
                raise transition_error
        except WORKER_OPERATION_ERRORS as exc:
            raise DistillationWorkerCycleError(
                "amphora_timeout_recovery_failed"
            ) from exc

    def _mark_amphora_failed(
        self,
        identifier: str,
        error: str,
        *,
        task: Dict | None = None,
    ):
        """Commit one Amphora failure and sign terminal evidence only if it exhausted."""
        try:
            from core.kia import amphora

            transition = amphora.mark_failed_with_transition(
                identifier,
                error,
                expected_started_at=(
                    str(task.get("started_at") or "")
                    if task is not None
                    else None
                ),
            )
        except WORKER_OPERATION_ERRORS as exc:
            raise DistillationWorkerCycleError(
                "amphora_failure_transition_failed"
            ) from exc
        if transition is None:
            raise DistillationWorkerCycleError(
                "amphora_failure_transition_unmatched"
            )
        if transition.terminal:
            terminal_task = task or {}
            try:
                self._archive_failed_task_data(
                    transition.task_id,
                    terminal_task,
                    error,
                )
            except WORKER_STORAGE_ERRORS as exc:
                logger.error(
                    "[Hephaestus] 失败归档写入失败，但不阻断已提交终态证据 %s: %s",
                    transition.task_id,
                    exc,
                    exc_info=True,
                )
            try:
                self.reconcile_failed_terminal_receipts(
                    identifier=transition.task_id,
                )
            except DistillationWorkerCycleError:
                logger.error(
                    "[Hephaestus] failed-terminal outbox replay deferred",
                    exc_info=True,
                )
        return transition

    def _record_terminal_runtime_receipt(self, task: Dict, receipt) -> dict | None:
        """Record an exact terminal flow receipt after Amphora has committed it."""
        if not getattr(receipt, "terminal", False):
            return None
        try:
            from core.ops.cognitive_pipeline_receipts import record_distillation_terminal

            evidence = record_distillation_terminal(
                self.config,
                task=task,
                receipt=receipt,
            )
            if not evidence.get("matched"):
                logger.warning(
                    "[Hephaestus] terminal runtime receipt not matched for task %s: %s",
                    str(task.get("task_id") or task.get("session_id") or ""),
                    evidence.get("reason", "unknown"),
                )
                return None
            return evidence
        except (
            AttributeError,
            ImportError,
            KeyError,
            OSError,
            RuntimeError,
            TypeError,
            ValueError,
            sqlite3.Error,
        ):
            # The durable Amphora receipt is already committed.  Keep the task
            # terminal and let the explicit reconciler repair ledger evidence.
            logger.error("[Hephaestus] terminal runtime receipt failed", exc_info=True)
            return None

    def reconcile_terminal_receipts(
        self,
        *,
        identifier: str | None = None,
        limit: int = 100,
    ) -> int:
        """Replay committed/skip Amphora terminal outbox entries idempotently."""
        try:
            from core.kia import amphora
        except ImportError as exc:
            raise DistillationWorkerCycleError(
                "amphora_terminal_reconciliation_unavailable"
            ) from exc
        try:
            pending = amphora.list_terminal_receipt_outbox(
                identifier=identifier,
                limit=limit,
            )
        except WORKER_OPERATION_ERRORS as exc:
            raise DistillationWorkerCycleError(
                "amphora_terminal_outbox_scan_failed"
            ) from exc
        reconciled = 0
        for item in pending:
            task = item.get("task") or {}
            outbox = item.get("outbox") or {}
            receipt = item.get("receipt")
            evidence = self._record_terminal_runtime_receipt(task, receipt)
            if not evidence:
                continue
            try:
                committed = amphora.mark_terminal_receipt_outbox_committed(
                    str(task.get("task_id") or ""),
                    expected_created_at=str(outbox.get("created_at") or ""),
                    runtime_receipt_id=str(
                        evidence.get("runtime_receipt_id") or ""
                    ),
                    production_event_id=str(
                        evidence.get("production_event_id") or ""
                    ),
                    generation_id=str(evidence.get("generation_id") or ""),
                    config=self.config,
                )
            except WORKER_OPERATION_ERRORS:
                logger.error(
                    "[Hephaestus] terminal outbox commit failed",
                    exc_info=True,
                )
                continue
            if committed:
                reconciled += 1
                self._mark_l1_distilled(str(task.get("session_id") or ""))
        return reconciled

    def _record_failed_terminal_runtime_receipt(
        self,
        task: Dict,
        reason: str,
    ) -> dict | None:
        """Record the exact failed terminal after a permanent, retry-exhausted failure."""
        try:
            from core.ops.cognitive_pipeline_receipts import record_distillation_failed_terminal

            evidence = record_distillation_failed_terminal(
                self.config,
                task=task or {},
                reason=reason,
            )
            if not evidence.get("matched"):
                logger.warning(
                    "[Hephaestus] failed-terminal runtime receipt not matched for task %s: %s",
                    str((task or {}).get("task_id") or (task or {}).get("session_id") or ""),
                    evidence.get("reason", "unknown"),
                )
                return None
            return evidence
        except (
            AttributeError,
            ImportError,
            KeyError,
            OSError,
            RuntimeError,
            TypeError,
            ValueError,
            sqlite3.Error,
        ):
            # The durable Amphora failure is already committed.  Keep the task
            # failed and let the explicit reconciler repair ledger evidence.
            logger.error("[Hephaestus] failed-terminal runtime receipt failed", exc_info=True)
            return None

    def reconcile_failed_terminal_receipts(
        self,
        *,
        identifier: str | None = None,
        limit: int = 100,
    ) -> int:
        """Replay durable Amphora failed-terminal outbox entries idempotently."""
        try:
            from core.kia import amphora
        except ImportError as exc:
            raise DistillationWorkerCycleError(
                "amphora_failed_terminal_reconciliation_unavailable"
            ) from exc
        try:
            pending = amphora.list_failed_terminal_receipt_outbox(
                identifier=identifier,
                limit=limit,
            )
        except WORKER_OPERATION_ERRORS as exc:
            raise DistillationWorkerCycleError(
                "amphora_failed_terminal_outbox_scan_failed"
            ) from exc
        reconciled = 0
        for item in pending:
            task = item.get("task") or {}
            outbox = item.get("outbox") or {}
            evidence = self._record_failed_terminal_runtime_receipt(
                task,
                str(outbox.get("reason") or ""),
            )
            if not evidence:
                continue
            try:
                committed = amphora.mark_failed_terminal_receipt_outbox_committed(
                    str(task.get("task_id") or ""),
                    expected_created_at=str(outbox.get("created_at") or ""),
                    runtime_receipt_id=str(
                        evidence.get("runtime_receipt_id") or ""
                    ),
                    production_event_id=str(
                        evidence.get("production_event_id") or ""
                    ),
                    generation_id=str(evidence.get("generation_id") or ""),
                    config=self.config,
                )
            except WORKER_OPERATION_ERRORS:
                logger.error(
                    "[Hephaestus] failed-terminal outbox commit failed",
                    exc_info=True,
                )
                continue
            if committed:
                reconciled += 1
        return reconciled

    def process_one(self, session_id: str) -> bool:
        """处理指定 session_id 的蒸馏任务（从 amphora 队列查找）。"""
        try:
            from core.kia import amphora
        except ImportError as exc:
            raise DistillationWorkerCycleError("amphora_import_unavailable") from exc

        try:
            task = amphora.claim_task(session_id)
            if task is not None:
                return self.process_one_task(task)
        except WORKER_OPERATION_ERRORS as exc:
            raise DistillationWorkerCycleError("amphora_task_claim_failed") from exc

        logger.warning("任务不存在于 amphora 队列: %s", session_id)
        return False

    def process_one_task(self, task: Dict) -> bool:
        """处理单个蒸馏任务（从 amphora SQLite 队列）

        Args:
            task: amphora 任务字典，包含 session_id, messages, meta, retry_count 等

        Returns:
            是否成功处理或委托
        """
        session_id = task.get("session_id")
        task_id = str(task.get("task_id") or "")
        if not session_id:
            logger.warning("任务缺少 session_id")
            return False

        # 检查蒸馏是否被暂停
        try:
            from core.hephaestus.distillation_engine import is_distillation_paused

            if is_distillation_paused():
                logger.info("[Hephaestus] 蒸馏当前处于暂停状态，跳过任务: %s", session_id)
                return False
        except (
            OSError,
            ValueError,
            TypeError,
            KeyError,
            ImportError,
            AttributeError,
            RuntimeError,
        ) as exc:
            raise DistillationWorkerCycleError(
                "distillation_pause_state_unavailable"
            ) from exc

        self._emit_progress(session_id, "started", "正在提炼知识...")

        # 检查重试次数
        retry_count = int(task.get("retry_count") or 0)
        max_retries = int(task.get("max_retries") or 3)
        if retry_count >= max_retries:
            logger.error(
                "任务重试次数耗尽 (%s/%s)，标记为失败: %s",
                retry_count,
                max_retries,
                session_id,
            )
            self._mark_amphora_failed(
                task_id or session_id,
                "重试次数耗尽，Agent持续不可用",
                task=task,
            )
            return False

        # 构建任务
        distill_task = QueueDistillTask(
            session_id=session_id,
            messages=_normalize_messages(task.get("messages", [])),
            meta={
                **(task.get("meta", {}) or {}),
                "_amphora_task_id": task_id,
                "input_revision": str(task.get("input_revision") or ""),
            },
        )

        # Mnemos 直接调用 LLM API 执行蒸馏（主备链 failover）
        logger.info("[Hephaestus] 使用 API 模式执行蒸馏: %s", session_id)
        return self._sync_distill_and_complete(session_id, distill_task, task=task)

    def _run_distillation_engine(self, session_id: str, distill_task: QueueDistillTask):
        """在线程中执行蒸馏引擎调用，便于设置总超时。"""
        from core.hephaestus.distillation_engine import (
            DistillationEngine,
            HttpApiHostAgentCaller,
        )
        from core.mnemos_bus import get_event_bus

        caller = HttpApiHostAgentCaller()
        engine = DistillationEngine(
            caller=caller,
            receipt_config=self.config,
            event_bus=get_event_bus(config=self.config),
        )
        result = engine.process(
            session_id=session_id,
            messages=distill_task.messages,
            meta=distill_task.meta,
        )
        return engine, result

    def _estimate_distill_task_tokens(self, distill_task: QueueDistillTask) -> int:
        """估算任务输入规模，用于选择不会误杀长任务的外层超时。"""
        try:
            from core.hephaestus.tokenizer import get_tokenizer

            tokenizer = get_tokenizer()
            return sum(
                tokenizer.estimate(str(message.get("content", "")))
                for message in distill_task.messages
                if isinstance(message, dict)
            )
        except (ImportError, AttributeError, RuntimeError, ValueError):
            return sum(
                max(1, len(str(message.get("content", ""))) // 4)
                for message in distill_task.messages
                if isinstance(message, dict)
            )

    def _resolve_task_timeout_seconds(
        self, distill_task: QueueDistillTask
    ) -> tuple[float, int, str]:
        """按输入规模解析任务级超时，避免长蒸馏在即将成功前被 300s 固定值丢弃。"""

        def _cfg_float(key: str, default: float) -> float:
            try:
                return float(self.config.get(key, default) or default)
            except (TypeError, ValueError):
                return default

        def _cfg_int(key: str, default: int) -> int:
            try:
                return int(self.config.get(key, default) or default)
            except (TypeError, ValueError):
                return default

        base = max(0.001, _cfg_float("distill.task_timeout_seconds", 300.0))
        medium = max(base, _cfg_float("distill.task_timeout_medium_seconds", 900.0))
        long = max(medium, _cfg_float("distill.task_timeout_long_seconds", 1800.0))
        chunked = max(long, _cfg_float("distill.task_timeout_chunked_seconds", 3600.0))

        short_threshold = max(0, _cfg_int("distill.response_tokens_short_input_threshold", 6000))
        medium_threshold = max(
            short_threshold, _cfg_int("distill.response_tokens_medium_input_threshold", 16000)
        )
        token_budget_total = max(1, _cfg_int("distill.token_budget_total", 16000))
        try:
            chunk_std_factor = float(self.config.get("distill.chunk_std_factor", 3) or 3)
        except (TypeError, ValueError):
            chunk_std_factor = 3.0
        chunk_threshold = max(medium_threshold, int(token_budget_total * chunk_std_factor))

        estimated_tokens = self._estimate_distill_task_tokens(distill_task)
        if estimated_tokens > chunk_threshold:
            return chunked, estimated_tokens, "chunked"
        if estimated_tokens > medium_threshold:
            return long, estimated_tokens, "long"
        if estimated_tokens > short_threshold:
            return medium, estimated_tokens, "medium"
        return base, estimated_tokens, "default"

    def _quarantine_timed_out_future(
        self,
        *,
        runner: threading.Thread,
        future: Future,
        session_id: str,
        identifier: str,
        task: Dict | None,
        timeout: float,
    ) -> None:
        """Keep a timed-out generation owned until its live thread finishes.

        Python cannot cancel a running thread.  Advancing Amphora to retry while
        this future is still alive would permit two generations to execute
        concurrently.  The task therefore remains ``processing`` and consumes
        no retry until the late computation has actually stopped.
        """

        if not hasattr(self, "_late_futures_lock"):
            self._late_futures_lock = threading.Lock()
        if not hasattr(self, "_late_futures"):
            self._late_futures = {}
        key = str(identifier or session_id)
        task_id = str((task or {}).get("task_id") or identifier or "")
        started_at = str((task or {}).get("started_at") or "")
        if not task_id or not started_at:
            raise DistillationWorkerCycleError(
                "distillation_timeout_claim_identity_missing"
            )
        with self._late_futures_lock:
            if key in self._late_futures:
                raise DistillationWorkerCycleError(
                    "distillation_timeout_generation_already_owned"
                )
            self._late_futures[key] = (runner, future)
            self._late_claims[key] = (task_id, started_at)

        def finalize_late_generation(completed: Future) -> None:
            try:
                completed.result()
            except BaseException:
                # The late result is deliberately discarded.  Its exception is
                # represented by the one Amphora failure transition below.
                pass
            transition_committed = False
            failure_reason = f"同步蒸馏任务超时 (>{timeout}s)"
            try:
                self._emit_progress(
                    session_id,
                    "failed",
                    f"同步蒸馏任务超时后已停止 (>{timeout}s)",
                )
                self._mark_amphora_failed(
                    identifier or session_id,
                    failure_reason,
                    task=task,
                )
                transition_committed = True
            except DistillationWorkerCycleError:
                logger.error(
                    "[Hephaestus] 超时任务停止后写入失败转换失败: %s",
                    session_id,
                    exc_info=True,
                )
                with self._late_futures_lock:
                    self._late_transition_failures[key] = (
                        identifier or session_id,
                        failure_reason,
                        task,
                    )
            finally:
                with self._late_futures_lock:
                    self._late_futures.pop(key, None)
                    if transition_committed:
                        self._late_claims.pop(key, None)

        future.add_done_callback(finalize_late_generation)

    def _submit_distillation_future(
        self,
        session_id: str,
        distill_task: QueueDistillTask,
    ) -> tuple[Future, threading.Thread]:
        """Run one distillation in a daemon thread with a standard Future."""

        future: Future = Future()

        def run() -> None:
            if not future.set_running_or_notify_cancel():
                return
            try:
                future.set_result(
                    self._run_distillation_engine(session_id, distill_task)
                )
            except BaseException as exc:
                future.set_exception(exc)

        runner = threading.Thread(
            target=run,
            name=f"mnemos-distill-{session_id[:32]}",
            daemon=True,
        )
        runner.start()
        return future, runner

    def _sync_distill_and_complete(
        self,
        session_id: str,
        distill_task: QueueDistillTask,
        *,
        task_id: str = "",
        task: Dict | None = None,
    ) -> bool:
        """同步执行 API 蒸馏，不依赖外部 Agent 异步处理。"""
        task_id = task_id or str((distill_task.meta or {}).get("_amphora_task_id") or "")
        self._emit_progress(session_id, "extracting", "同步蒸馏中（API 模式）...")
        try:
            from core.hephaestus.distillation_engine import (
                DistillationAPIError,
                generate_distillation_error_report,
                pause_distillation,
            )

            timeout, input_tokens, timeout_tier = self._resolve_task_timeout_seconds(distill_task)
            logger.info(
                "[Hephaestus] 蒸馏任务超时预算 %s: %.1fs (tier=%s, input_tokens=%s)",
                session_id,
                timeout,
                timeout_tier,
                input_tokens,
            )
            future, runner = self._submit_distillation_future(
                session_id,
                distill_task,
            )
            try:
                engine, result = future.result(timeout=timeout)
            except FutureTimeoutError:
                logger.error("[Hephaestus] 同步蒸馏任务超时 %s (> %ss)", session_id, timeout)
                self._emit_progress(
                    session_id,
                    "timeout_quarantined",
                    f"同步蒸馏任务超时，等待当前执行停止 (>{timeout}s)",
                )
                self._quarantine_timed_out_future(
                    runner=runner,
                    future=future,
                    session_id=session_id,
                    identifier=task_id or session_id,
                    task=task,
                    timeout=timeout,
                )
                try:
                    pause_distillation(
                        reason=f"蒸馏任务超时: {session_id}",
                        last_error=f"同步蒸馏任务超时 (>{timeout}s)",
                    )
                except WORKER_OPERATION_ERRORS as pause_err:
                    logger.error("[Hephaestus] 超时后暂停蒸馏失败: %s", pause_err, exc_info=True)
                return False

            if result.judgment == "error":
                # [P001] API 内部错误被 engine.process 标记为 error，必须重试而不是 done
                error_msg = result.judgment_reason or "蒸馏引擎返回 error 判定"
                logger.error("[Hephaestus] 同步蒸馏返回 error %s: %s", session_id, error_msg)
                self._emit_progress(session_id, "failed", f"蒸馏引擎错误: {error_msg}")
                self._mark_amphora_failed(task_id or session_id, error_msg, task=task)
                return False

            receipt = engine.write_pages_with_receipt(result)
            written = list(receipt.written_pages)
            skill_asset_receipt = getattr(result, "cognition_asset_receipt", None)
            asset_bearing_result = bool(
                result.judgment == "knowledge"
                or (
                    result.judgment == "skill"
                    and skill_asset_receipt is not None
                    and skill_asset_receipt.committed
                )
            )
            if asset_bearing_result and result.fragments:
                # The engine write boundary owns durable cognition dispatch.
                # Re-emitting here would create a second projection producer.
                logger.info(
                    "[Hephaestus] 同步蒸馏结果 %s: 判定=%s, 状态=%s, 写入 %s 页",
                    session_id,
                    result.judgment,
                    receipt.status,
                    len(written),
                )
                self._emit_progress(
                    session_id,
                    receipt.status,
                    f"同步蒸馏状态={receipt.status}，持久产物={len(written)}",
                )
            else:
                logger.info(
                    "[Hephaestus] 同步蒸馏结果 %s: 判定=%s, 状态=%s",
                    session_id,
                    result.judgment,
                    receipt.status,
                )
                self._emit_progress(
                    session_id,
                    receipt.status,
                    f"同步蒸馏状态={receipt.status}，原因={receipt.terminal_reason}",
                )

            # Persist the typed receipt; only committed/intentional_skip become terminal.
            try:
                from core.kia import amphora

                if not amphora.mark_terminal(
                    task_id or session_id,
                    receipt,
                    expected_started_at=(
                        str(task.get("started_at") or "")
                        if task is not None
                        else None
                    ),
                ):
                    raise RuntimeError("amphora task receipt update matched no task")
            except WORKER_OPERATION_ERRORS as exc:
                logger.error(
                    "[Hephaestus] terminal receipt 写入失败 %s: %s", session_id, exc, exc_info=True
                )
                return False

            if receipt.terminal:
                try:
                    self.reconcile_terminal_receipts(
                        identifier=task_id or session_id,
                    )
                except DistillationWorkerCycleError:
                    logger.error(
                        "[Hephaestus] terminal outbox replay deferred",
                        exc_info=True,
                    )

            return receipt.status in {"committed", "intentional_skip", "proposal_pending"}

        except DistillationAPIError as e:
            logger.error("[Hephaestus] 同步蒸馏 API 全部失败 %s: %s", session_id, e, exc_info=True)
            self._emit_progress(session_id, "api_failed", f"所有 LLM API 不可用: {e}")
            # 生成错误报告并弹窗 Obsidian
            try:
                report_path = generate_distillation_error_report(e)
                logger.warning("[Hephaestus] 错误报告已生成: %s", report_path)
            except WORKER_OPERATION_ERRORS as report_err:
                logger.error("[Hephaestus] 生成错误报告失败: %s", report_err, exc_info=True)
            # 暂停蒸馏（自动倒计时恢复）
            try:
                pause_distillation(
                    reason=f"所有 LLM API 不可用: {e}",
                    api_chain_desc=e.chain_desc,
                    last_error=str(e),
                )
            except WORKER_OPERATION_ERRORS as pause_err:
                logger.error("[Hephaestus] 暂停蒸馏失败: %s", pause_err, exc_info=True)
            self._mark_amphora_failed(task_id or session_id, str(e), task=task)
            return False
        except WORKER_STORAGE_ERRORS as e:
            logger.error("[Hephaestus] 同步蒸馏失败 %s: %s", session_id, e, exc_info=True)
            self._emit_progress(session_id, "failed", f"同步蒸馏失败: {e}")
            self._mark_amphora_failed(task_id or session_id, str(e), task=task)
            return False

    def _emit_progress(self, session_id: str, stage: str, message: str):
        """发送蒸馏进度事件，不影响主流程"""
        if stage == "completed" and session_id in self._completed_notified:
            return
        if stage == "completed":
            self._completed_notified.add(session_id)
            # [P1-18] 防止内存泄漏：集合超过上限时清理最旧的 50%
            if len(self._completed_notified) > self._COMPLETED_NOTIFIED_MAX:
                # 简单策略：清空后重新积累（这些通知只是去重用的）
                self._completed_notified.clear()

        progress_map = {
            "started": 0,
            "judged": 25,
            "extracted": 50,
            "completed": 100,
            "skipped": 0,
        }
        payload = {
            "session_id": session_id,
            "stage": stage,
            "status": stage,
            "progress_pct": progress_map.get(stage, 0),
            "message": message,
            "timestamp": datetime.now().isoformat(),
        }
        try:
            from core.mnemos_bus import publish_event

            publish_event("distillation_progress", "hephaestus_worker", payload)
        except WORKER_OPERATION_ERRORS as exc:
            logger.debug("发送蒸馏进度事件失败: %s", exc, exc_info=True)

        try:
            from core.kia import amphora

            amphora_step = {
                "started": amphora.DistillProgress.EXTRACTING.value,
                "judged": amphora.DistillProgress.STRUCTURING.value,
                "extracted": amphora.DistillProgress.VERIFYING.value,
                "completed": amphora.DistillProgress.DONE.value,
                "skipped": amphora.DistillProgress.DONE.value,
            }.get(stage)
            if amphora_step:
                amphora.update_progress(session_id, amphora_step, message)
        except (
            OSError,
            ValueError,
            TypeError,
            KeyError,
            ImportError,
            AttributeError,
            RuntimeError,
        ):
            logger.warning("Unexpected error in hephaestus_worker.py", exc_info=True)

    def _archive_failed_task_data(self, session_id: str, task_data: dict, reason: str):
        """归档失败的任务数据（amphora 队列版本）"""
        failed_dir = self.archive_dir / "failed"
        archived_task = {
            **task_data,
            "failed_at": datetime.now().isoformat(),
            "fail_reason": reason,
            "archive_task_id": session_id,
        }
        task_component = hashlib.sha256(
            f"mnemos.failed_task_archive.v1\0{session_id}".encode("utf-8")
        ).hexdigest()
        failed_name = f"task-{task_component}.json"
        # trusted-scan: artifact owner=hephaestus target=failed_task_archive expires=never
        failed_path = secure_publish_immutable_text(
            failed_dir,
            failed_name,
            json.dumps(
                archived_task,
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
            ),
        )
        logger.warning("任务已归档到 failed: %s", failed_path)

    def get_pending_count(self) -> int:
        """获取待处理任务数量（从 amphora SQLite 队列）"""
        try:
            from core.kia import amphora

            return len(amphora.list_pending(include_future_retry=False))
        except WORKER_OPERATION_ERRORS as exc:
            raise DistillationWorkerCycleError(
                "amphora_pending_count_unavailable"
            ) from exc

    def get_stats(self) -> dict:
        """获取 Worker 统计信息"""
        return {
            "pending": self.get_pending_count(),
            "queue_dir": str(self.queue_dir),
            "inbox_dir": str(self.inbox_dir),
            "archive_dir": str(self.archive_dir),
        }

    def stop(self) -> None:
        """请求停止 watch_queue 监控循环"""
        self._stop_event.set()
        logger.info("[Hephaestus] 已请求停止监控")

    def watch_queue(
        self, interval: float | None = None, callback: Optional[Callable] = None
    ) -> (
        None
    ):  # noqa: Vulture - public worker lifecycle loop used by operators and external scripts.
        """轮询 amphora 蒸馏队列并处理待蒸馏任务。

        Args:
            interval: 轮询间隔（秒），默认读取 distill.poll_interval_seconds
            callback: 可选回调函数，每次处理完调用
        """
        if interval is None:
            interval = float(self.config.get("distill.poll_interval_seconds", 60) or 60)
        logger.info("[Hephaestus] 开始轮询蒸馏队列 (间隔 %ss)", interval)

        while not self._stop_event.is_set():
            try:
                count = self.process_all()
                if count > 0:
                    logger.info("[Hephaestus] 轮询处理 %s 个任务", count)
                    if callback:
                        callback(count)
            except WORKER_STORAGE_ERRORS as e:
                logger.error("[Hephaestus] 轮询处理失败: %s", e, exc_info=True)

            if self._stop_event.wait(interval):
                break
