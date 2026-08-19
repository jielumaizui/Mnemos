"""
Chronos — 时间之神 — KIA 步骤调度中心

事件驱动 + 按需触发 + 并行执行架构（ADR-020）。

16 个步骤按触发方式分为：
- 事件触发（实时响应，不经调度器）：connect_worker, iteration_tracker, task_classifier, aegis
- 定时触发（按 cron 节奏运行）：immune, dna, entropy, profile, capsule, shadow_page, stress_test
- 条件触发（满足条件才执行）：cognitive_decision_flywheel（legacy step name: skill_flywheel）
- 被动调用（工具函数）：time_parser
- 调度中心自身：knowledge_sched

同时保留原有任务调度/提醒功能（ScheduledTask）。

调度器仅隔离已知 I/O、配置、SQLite、存储与任务运行时故障；未知编程错误保持可见。
"""

from __future__ import annotations

import logging
import hashlib
import json
import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Callable, cast, ClassVar, Dict, List, Mapping, Optional

from core.cognitive.decision_trace import (
    MaterialActionAuthorization,
    MaterialActionCoordinator,
    MaterialActionPermit,
    MaterialActionRequest,
    MaterialActionTerminal,
    authorize_exact_project_contract_action,
    find_pending_material_action_authorization,
    require_material_action,
)
from core.cognitive.state_contract import sha256_json
from core.cognitive.state_store import CognitiveStateStore
from core.kia.chronos_builtin_steps import ChronosBuiltinStepMixin
from core.kia.chronos_contracts import (  # noqa: F401
    CHRONOS_DECISION_CONTRACT_ID,
    CHRONOS_DECISION_CONTRACT_REVISION,
    CHRONOS_DECISION_CONTRACT_TEXT,
    CHRONOS_DECISION_PRODUCER_HASH,
    CHRONOS_EXECUTOR,
    CHRONOS_OPERATION_ERRORS,
    CHRONOS_OWNER,
    CHRONOS_STEP_EXECUTE_ACTION,
    CHRONOS_STEP_SUCCESS_STATUSES,
    CHRONOS_TASK_CREATE_ACTION,
    CRON_DOW_DAYS,
    CRON_TRIGGER__MATCHES_CRON_DOW_DAYS,
    ConditionTrigger,
    CronTrigger,
    EventTrigger,
    KNOWLEDGE_SCHEDULER_CLEANUP_OLD_TASKS_DAYS,
    KNOWLEDGE_SCHEDULER_DURATION_BUCKET_MONTH_DAYS,
    KNOWLEDGE_SCHEDULER_DURATION_BUCKET_QUARTER_DAYS,
    KNOWLEDGE_SCHEDULER_DURATION_BUCKET_WEEK_DAYS,
    KNOWLEDGE_SCHEDULER_DURATION_BUCKET_YEAR_DAYS,
    KNOWLEDGE_SCHEDULER__REMINDER_DATE_FOR_DAYS_UNTIL_DAYS,
    KNOWLEDGE_SCHEDULER__REMINDER_DATE_FOR_DAYS_UNTIL_DAYS_2,
    KNOWLEDGE_SCHEDULER__ROW_TO_TASK_ROW,
    PassiveTrigger,
    REMINDER_DAYS,
    STATS_DAYS,
    ScheduledStep,
    ScheduledStepEffectOracle,
    ScheduledTask,
    ScheduledTaskEffectOracle,
    TIMEOUT_SECONDS,
    TIMEOUT_SECONDS_2,
    Trigger,
    scheduled_step_material_action_binding,
    scheduled_task_material_action_binding,
)
from core.kia.chronos_scheduler_support import SchedulerSupportMixin

logger = logging.getLogger(__name__)

# ============================================================
# KnowledgeScheduler — KIA 步骤调度中心
# ============================================================


class KnowledgeScheduler(ChronosBuiltinStepMixin, SchedulerSupportMixin):
    """
    KIA 步骤调度中心。

    职责：
    1. 注册所有 KIA 步骤及其触发条件
    2. 按触发条件筛选待执行步骤
    3. 处理依赖关系（拓扑排序）
    4. 无依赖步骤并行执行，有依赖步骤串行等待
    5. 记录执行日志和性能指标
    6. 连续 3 次失败自动禁用步骤

    注意：本调度器只管理**定时触发**和**条件触发**的步骤。
    **事件触发**的步骤由事件总线直接调用，不经过调度器。
    """

    MAX_CONSECUTIVE_FAILURES = 3
    # 停机追赶/启动补偿单次最多返回任务数，防止 backlog 拖垮调用方
    MAX_STARTUP_COMPENSATION_TASKS = 100
    # 每次 tick 最多执行步骤数，防止大量步骤同时到期导致资源 spike
    MAX_STEPS_PER_TICK = 4
    EVENT_TRIGGER_ROUTES: ClassVar[tuple[tuple[str, str], ...]] = (
        ("page_created", "page.created"),
        ("page_modified", "page.modified"),
        ("session_start", "session.start"),
        ("message_exchanged", "message.exchanged"),
    )
    _scheduled_step_type = ScheduledStep
    _cron_trigger_type = CronTrigger
    _condition_trigger_type = ConditionTrigger
    _event_trigger_type = EventTrigger

    def __init__(
        self,
        max_workers: int = 4,
        db_path: Optional[str] = None,
        *,
        material_action_resolver: (
            Callable[[Mapping[str, str]], MaterialActionAuthorization] | None
        ) = None,
        trusted_markdown_action_resolver: (
            Callable[[Mapping[str, str]], MaterialActionAuthorization] | None
        ) = None,
    ):
        # 调度器
        self.steps: Dict[str, ScheduledStep] = {}
        self.executor = ThreadPoolExecutor(max_workers=max_workers)
        self._results: Dict[str, Dict] = {}
        self._lock = threading.Lock()
        self._event_handlers_registered = False
        self._material_action_resolver = material_action_resolver
        self._trusted_markdown_action_resolver = trusted_markdown_action_resolver

        # 任务调度/提醒（原有功能）
        if db_path:
            self.DB_PATH = Path(db_path).expanduser()
        else:
            from core.config import get_config

            self.DB_PATH = get_config().database_dir / "live_sync.db"
        self._init_task_db()

    def _resolve_material_action(
        self,
        binding: Mapping[str, str],
        command_ids: Mapping[str, str] | None,
        *,
        action_type: str,
        source_facts: Mapping[str, Any],
        task: str,
        goal: str,
        approved_candidate_key: str,
        approved_candidate_summary: str,
        rejected_candidate_key: str,
        rejected_candidate_summary: str,
        committed_metric: str,
        rejected_metric: str,
    ) -> MaterialActionAuthorization:
        if self._material_action_resolver is not None:
            return self._material_action_resolver(binding)
        if isinstance(command_ids, Mapping):
            command_id = str(command_ids.get(binding["target_ref"]) or "").strip()
            if not command_id:
                raise PermissionError("Chronos action lacks its exact material command")
            return MaterialActionCoordinator(
                CognitiveStateStore(self.DB_PATH.parent)
            ).bind_for_recovery(
                command_id,
                executor_id=CHRONOS_EXECUTOR,
            )
        state_db_path = (self.DB_PATH.parent / "producer_consumer_ledger.db").resolve(strict=False)
        request = MaterialActionRequest(
            owner=CHRONOS_OWNER,
            executor_id=CHRONOS_EXECUTOR,
            action_type=action_type,
            target_ref=str(binding["target_ref"]),
            input_hash=str(binding["input_hash"]),
            expected_state_db=str(state_db_path),
        )
        pending = find_pending_material_action_authorization(
            state_db_path=state_db_path,
            owner=request.owner,
            executor_id=request.executor_id,
            action_type=request.action_type,
            target_ref=request.target_ref,
            input_hash=request.input_hash,
        )
        if pending is not None:
            return pending
        decision_created_at = datetime.now().astimezone().isoformat()
        return authorize_exact_project_contract_action(
            expected_request=request,
            state_db_path=state_db_path,
            contract_id=CHRONOS_DECISION_CONTRACT_ID,
            contract_revision_id=CHRONOS_DECISION_CONTRACT_REVISION,
            contract_text=CHRONOS_DECISION_CONTRACT_TEXT,
            source_namespace="chronos-material-action",
            source_facts={
                **dict(source_facts),
                "decision_created_at": decision_created_at,
            },
            decision_checks={
                "registered_chronos_action": action_type
                in {CHRONOS_TASK_CREATE_ACTION, CHRONOS_STEP_EXECUTE_ACTION},
                "material_binding_complete": bool(
                    binding.get("target_ref") and binding.get("input_hash")
                ),
                "scheduler_preconditions_satisfied": (
                    bool(source_facts.get("registered")) and bool(source_facts.get("enabled"))
                    if action_type == CHRONOS_STEP_EXECUTE_ACTION
                    else bool(str(source_facts.get("task_type") or "").strip())
                    and bool(str(source_facts.get("due_date") or "").strip())
                ),
            },
            evidence_refs=(
                f"chronos-target:{binding['target_ref']}",
                f"chronos-input:{binding['input_hash']}",
            ),
            task=task,
            goal=goal,
            constraints=(
                "The target and input must match the current scheduler request.",
                "A scheduled step must be registered and enabled before execution.",
            ),
            created_at=decision_created_at,
            producer="chronos-knowledge-scheduler",
            producer_version=CHRONOS_DECISION_CONTRACT_REVISION,
            producer_code_hash=CHRONOS_DECISION_PRODUCER_HASH,
            evaluator_id="chronos-material-action-evaluator",
            approved_candidate_key=approved_candidate_key,
            approved_candidate_summary=approved_candidate_summary,
            rejected_candidate_key=rejected_candidate_key,
            rejected_candidate_summary=rejected_candidate_summary,
            approved_reason_code="chronos_exact_action_verified",
            rejected_reason_code="chronos_exact_action_rejected",
            committed_metric=committed_metric,
            rejected_metric=rejected_metric,
        )

    def _scheduled_task_effect_hash(self, task_id: str) -> str:
        with sqlite3.connect(str(self.DB_PATH), timeout=10) as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute(
                "SELECT * FROM knowledge_scheduled_tasks WHERE task_id=?",
                (task_id,),
            ).fetchone()
        return cast(
            str,
            sha256_json({"scheduled_task": dict(row) if row is not None else None}),
        )

    def _scheduled_step_effect_hash(self, step_name: str) -> str:
        with sqlite3.connect(str(self.DB_PATH), timeout=10) as conn:
            return self._scheduled_step_effect_hash_conn(conn, step_name)

    @staticmethod
    def _scheduled_step_effect_hash_conn(
        conn: sqlite3.Connection,
        step_name: str,
    ) -> str:
        """Hash the business target, excluding diagnostic/attempt journals."""

        conn.row_factory = sqlite3.Row
        state = conn.execute(
            "SELECT * FROM scheduler_step_state WHERE step_name=?",
            (step_name,),
        ).fetchone()
        return cast(
            str,
            sha256_json({"step_state": dict(state) if state is not None else None}),
        )

    def shutdown(self):
        """关闭调度器，释放线程池资源。"""
        if hasattr(self, "executor") and self.executor:
            self.executor.shutdown(wait=True)

    def __del__(self):
        self.shutdown()

    # ----------------------------------------------------------
    # 步骤注册
    # ----------------------------------------------------------

    # ----------------------------------------------------------
    # tick — 调度器主循环
    # ----------------------------------------------------------

    def tick(
        self,
        *,
        material_action_commands: Mapping[str, str] | None = None,
    ) -> Dict[str, Dict]:
        """
        一次调度 tick。

        1. 筛选满足触发条件的步骤
        2. 拓扑排序（依赖优先）
        3. 无依赖步骤并行执行
        4. 有依赖步骤串行等待
        5. 记录结果、处理失败

        Returns:
            {step_name: result_dict}
        """
        # 1. 筛选满足触发条件的步骤（排除事件触发和被动触发）
        ready = [s for s in self.steps.values() if s.enabled and s.trigger.is_due()]

        if not ready:
            return {}

        logger.info("调度 tick: %s 个步骤待执行", len(ready))

        # 2. 拓扑排序
        ordered = self._topological_sort(ready)

        # [P108] 限制单次 tick 执行步骤数，防止停机后大量步骤同时到期拖垮系统
        if len(ordered) > self.MAX_STEPS_PER_TICK:
            ordered = ordered[: self.MAX_STEPS_PER_TICK]
            logger.info(
                "调度 tick 步骤数超过阈值 %s，本次仅执行前 %s 个",
                self.MAX_STEPS_PER_TICK,
                self.MAX_STEPS_PER_TICK,
            )

        resource_deferrals = self._resource_budget_deferrals(ordered)
        if resource_deferrals is not None:
            with self._lock:
                self._results.update(resource_deferrals)
            return resource_deferrals

        # 3. 分离无依赖和有依赖
        parallel = [s for s in ordered if not s.deps]
        sequential = [s for s in ordered if s.deps]

        results: Dict[str, Dict] = {}

        # 4. 并行执行无依赖步骤
        if parallel:
            futures = {
                self.executor.submit(
                    self._run_step,
                    step,
                    material_action_commands=material_action_commands,
                ): step
                for step in parallel
            }
            for future in as_completed(futures):
                step = futures[future]
                try:
                    results[step.name] = future.result(timeout=step.timeout)
                except CHRONOS_OPERATION_ERRORS as e:
                    results[step.name] = {"status": "error", "error": str(e)}
                    self._handle_step_failure(step, e)

        # 5. 串行执行有依赖步骤
        for step in sequential:
            deps_ok = all(d in results and results[d].get("status") != "error" for d in step.deps)
            if not deps_ok:
                results[step.name] = {
                    "status": "skipped",
                    "reason": "dependencies_not_met",
                }
                continue
            try:
                results[step.name] = self._run_step(
                    step,
                    material_action_commands=material_action_commands,
                )
            except CHRONOS_OPERATION_ERRORS as e:
                results[step.name] = {"status": "error", "error": str(e)}
                self._handle_step_failure(step, e)

        # 6. 更新全局结果缓存
        with self._lock:
            self._results.update(results)

        return results

    def _resource_budget_deferrals(self, ordered: List[ScheduledStep]) -> Optional[Dict[str, Dict]]:
        """Return deferred results when the KIA scheduler budget is unavailable."""
        if not ordered:
            return None
        try:
            from core.resource_budget import get_budget

            budget = get_budget()
            if budget.can_run("kia_sched"):
                return None

            delay = budget.throttle_delay("kia_sched")
            status = budget.status()
            timestamp = datetime.now().isoformat()
            retry_after = max(1, int(delay or 60))
            logger.info(
                "调度 tick 因资源预算延后: state=%s retry_after=%ss steps=%s",
                status.get("state", "unknown"),
                retry_after,
                len(ordered),
            )
            return {
                step.name: {
                    "status": "deferred",
                    "reason": "resource_budget",
                    "resource_state": status.get("state", "unknown"),
                    "resource_status": status,
                    "retry_after_seconds": retry_after,
                    "_meta": {
                        "duration_sec": 0.0,
                        "timestamp": timestamp,
                    },
                }
                for step in ordered
            }
        except CHRONOS_OPERATION_ERRORS:
            logger.warning("资源预算检查失败，继续执行调度 tick", exc_info=True)
            return None

    def _run_step(
        self,
        step: ScheduledStep,
        *,
        material_action_commands: Mapping[str, str] | None = None,
    ) -> Dict:
        """执行单个步骤，包装日志和计时"""
        start = datetime.now()
        binding = scheduled_step_material_action_binding(step)
        registered_step = self.steps.get(step.name)
        if (
            self._material_action_resolver is None
            and not isinstance(material_action_commands, Mapping)
            and (registered_step is not step or not step.enabled)
        ):
            raise PermissionError("Chronos project contract requires an enabled registered step")
        func = step.func
        callable_ref = (
            f"{getattr(func, '__module__', type(func).__module__)}:"
            f"{getattr(func, '__qualname__', type(func).__qualname__)}"
        )
        authorization = self._resolve_material_action(
            binding,
            material_action_commands,
            action_type=CHRONOS_STEP_EXECUTE_ACTION,
            source_facts={
                "schema_version": "mnemos.chronos_step_decision_facts.v1",
                "step_name": step.name,
                "callable_ref": callable_ref,
                "trigger": step.trigger.describe(),
                "dependencies": sorted(step.deps),
                "timeout": int(step.timeout),
                "registered": registered_step is step,
                "enabled": bool(step.enabled),
            },
            task=f"Run registered Chronos step {step.name}",
            goal="Execute only the exact enabled scheduler step selected for this tick.",
            approved_candidate_key="run_registered_due_chronos_step",
            approved_candidate_summary=(
                "Run the exact enabled Chronos step registered for this scheduler."
            ),
            rejected_candidate_key="reject_unregistered_or_disabled_step",
            rejected_candidate_summary=(
                "Reject a callable that is not the current enabled registered step."
            ),
            committed_metric="chronos_step_terminal_receipt",
            rejected_metric="unregistered_chronos_step_execution_count",
        )
        permit = authorization.permit
        oracle = ScheduledStepEffectOracle(
            self.DB_PATH,
            step_name=step.name,
            input_hash=binding["input_hash"],
        )
        recovered = authorization.recover(oracle)
        if recovered is not None:
            if oracle.last_result is None and oracle.observe(permit) is None:
                raise RuntimeError("terminal scheduled-step receipt lacks target evidence")
            return dict(oracle.last_result or {})

        permit = require_material_action(
            authorization,
            owner=CHRONOS_OWNER,
            executor_id=CHRONOS_EXECUTOR,
            action_type=CHRONOS_STEP_EXECUTE_ACTION,
            target_ref=binding["target_ref"],
            input_hash=binding["input_hash"],
            expected_state_db=self.DB_PATH.parent / "producer_consumer_ledger.db",
        )
        before_hash = self._scheduled_step_effect_hash(step.name)
        self._begin_step_attempt(
            permit=permit,
            step=step,
            input_hash=binding["input_hash"],
            before_hash=before_hash,
            started_at=start,
        )
        try:
            result = step.func()
        except CHRONOS_OPERATION_ERRORS as e:
            duration = (datetime.now() - start).total_seconds()
            self._handle_step_failure(step, e)
            result = {
                "status": "error",
                "error": str(e),
                "_meta": {
                    "duration_sec": duration,
                    "timestamp": start.isoformat(),
                },
            }
        else:
            if not isinstance(result, dict):
                result = {"status": "ok", "result": str(result)}
            result["_meta"] = {
                "duration_sec": (datetime.now() - start).total_seconds(),
                "timestamp": start.isoformat(),
            }
            if result.get("status") in CHRONOS_STEP_SUCCESS_STATUSES:
                step.consecutive_failures = 0

        if result.get("status") == "deferred":
            logger.info(
                "步骤 %s 延后执行 (%s), 耗时 %.1fs",
                step.name,
                result.get("reason", "deferred"),
                result["_meta"]["duration_sec"],
            )
        else:
            logger.info(
                "步骤 %s 完成 (%s), 耗时 %.1fs",
                step.name,
                result.get("status"),
                result["_meta"]["duration_sec"],
            )
        expected_status = self._finalize_step_attempt(
            permit=permit,
            step=step,
            result=result,
            started_at=start,
        )
        recovered = authorization.recover(oracle)
        if recovered is None or recovered.status != expected_status:
            raise RuntimeError("scheduled-step target journal did not close its material command")
        return result

    def _begin_step_attempt(
        self,
        *,
        permit: MaterialActionPermit,
        step: ScheduledStep,
        input_hash: str,
        before_hash: str,
        started_at: datetime,
    ) -> None:
        empty_result: Dict[str, Any] = {}
        with sqlite3.connect(str(self.DB_PATH), timeout=10) as conn:
            conn.execute(
                """
                INSERT INTO scheduler_step_effects(
                    effect_id, command_id, step_name, input_hash,
                    before_hash, after_hash, status, reason_code,
                    result_json, result_hash, started_at, completed_at
                ) VALUES (?, ?, ?, ?, ?, ?, 'executing', '', ?, ?, ?, '')
                """,
                (
                    permit.effect_id,
                    permit.command_id,
                    step.name,
                    input_hash,
                    before_hash,
                    before_hash,
                    json.dumps(empty_result, sort_keys=True, separators=(",", ":")),
                    sha256_json(empty_result),
                    started_at.astimezone().isoformat(),
                ),
            )

    def _finalize_step_attempt(
        self,
        *,
        permit: MaterialActionPermit,
        step: ScheduledStep,
        result: Mapping[str, Any],
        started_at: datetime,
    ) -> str:
        success = str(result.get("status") or "") in CHRONOS_STEP_SUCCESS_STATUSES
        terminal_status = "committed" if success else "failed_terminal"
        reason_code = "" if success else f"chronos_step_{result.get('status', 'error')}"
        completed_at = datetime.now().astimezone().isoformat()
        result_payload = dict(result)
        result_json = json.dumps(
            result_payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        result_hash = sha256_json(result_payload)
        with sqlite3.connect(str(self.DB_PATH), timeout=10) as conn:
            conn.execute("BEGIN IMMEDIATE")
            if success:
                conn.execute(
                    """
                    INSERT INTO scheduler_step_state (step_name, last_run)
                    VALUES (?, ?)
                    ON CONFLICT(step_name) DO UPDATE SET last_run=excluded.last_run
                    """,
                    (step.name, completed_at),
                )
            conn.execute(
                """
                INSERT OR REPLACE INTO scheduler_step_log
                (step_name, started_at, duration_sec, status, error)
                VALUES (?, ?, ?, ?, ?)
                """,
                (
                    step.name,
                    started_at.isoformat(),
                    result_payload.get("_meta", {}).get("duration_sec", 0),
                    result_payload.get("status", "unknown"),
                    result_payload.get("error"),
                ),
            )
            after_hash = self._scheduled_step_effect_hash_conn(conn, step.name)
            cursor = conn.execute(
                """
                UPDATE scheduler_step_effects
                SET after_hash=?, status=?, reason_code=?, result_json=?,
                    result_hash=?, completed_at=?
                WHERE effect_id=? AND command_id=? AND status='executing'
                """,
                (
                    after_hash,
                    terminal_status,
                    reason_code,
                    result_json,
                    result_hash,
                    completed_at,
                    permit.effect_id,
                    permit.command_id,
                ),
            )
            if cursor.rowcount != 1:
                raise RuntimeError("scheduled-step effect intent is missing or already finalized")
        if success:
            step.trigger.update_last_run(completed_at)
        return terminal_status

    # ----------------------------------------------------------
    # 事件触发入口
    # ----------------------------------------------------------

    def trigger_event(self, event_type: str, payload: Dict | None = None) -> Dict:
        """
        事件触发入口 — 由事件总线调用。

        根据事件类型直接调用对应的 KIA 模块。
        """
        payload = payload or {}
        try:
            if event_type == "page.created":
                return cast(Dict, self._trigger_page_created(payload))
            if event_type == "page.modified":
                return self._trigger_page_modified(payload)
            if event_type == "session.start":
                return cast(Dict, self._trigger_session_start(payload))
            if event_type == "message.exchanged":
                return cast(Dict, self._trigger_message_exchanged(payload))
            return {"status": "unknown_event", "event_type": event_type}

        except CHRONOS_OPERATION_ERRORS as e:
            logger.error("事件触发执行失败 %s: %s", event_type, e)
            return {"status": "error", "error": str(e)}

    def _run_connect_worker(self, wiki_base: str) -> Dict:
        """连接Worker：全量构图，生成实体页面和MOC枢纽"""
        try:
            from core.kia.charon import run_connect_cycle

            result = run_connect_cycle(dry_run=False)
            return {
                "status": "ok",
                "people": result.get("people", 0),
                "projects": result.get("projects", 0),
                "tech": result.get("tech", 0),
                "concepts": result.get("concepts", 0),
                "mocs": result.get("mocs", 0),
            }
        except CHRONOS_OPERATION_ERRORS as e:
            logger.error("连接Worker失败: %s", e, exc_info=True)
            return {"status": "error", "error": str(e)}

    def _run_knowledge_evolution(self, wiki_base: str) -> Dict:
        """知识演化：扫描 wiki 版本并生成演化报告 + 新鲜度检查 + stale 标记"""
        try:
            from core.kia.proteus import IterationTracker, KnowledgeFreshnessChecker
            from core.hephaestus.evolution_tracker import TemporalEvolutionTracker

            # 1. 生成演化报告
            tracker = IterationTracker(wiki_base=wiki_base)
            report = tracker.scan_and_report(wiki_base)

            # 2. 新鲜度全库扫描（L4 轻量检查）
            checker = KnowledgeFreshnessChecker()
            freshness_alerts = checker.scan_all(wiki_base)
            if freshness_alerts:
                logger.info("[知识演化] 发现 %s 条过期知识", len(freshness_alerts))

            # 3. [P2-16] TemporalEvolutionTracker 驱动闭环：
            #    stale 标记 + 知识缺口提示 + EventBus 发射 + 每月限频主动蒸馏
            evolution_tracker = TemporalEvolutionTracker()
            evolution_alerts = evolution_tracker.scan_all_pages(Path(wiki_base))
            stale_count = sum(
                1
                for a in evolution_alerts
                if a.alert_type in ("version_outdated", "context_expired")
            )
            gap_count = sum(1 for a in evolution_alerts if a.alert_type == "rarely_accessed")
            if evolution_alerts:
                logger.info(
                    "[知识演化] EvolutionTracker: %s 条 alert (stale=%s, gap=%s)",
                    len(evolution_alerts),
                    stale_count,
                    gap_count,
                )

            return {
                "status": "ok",
                "reports": report.get("reports", 0),
                "topics": report.get("topics", []),
                "freshness_alerts": len(freshness_alerts),
                "evolution_alerts": len(evolution_alerts),
                "stale_marked": stale_count,
                "gaps_found": gap_count,
            }
        except CHRONOS_OPERATION_ERRORS as e:
            logger.error("知识演化失败: %s", e)
            return {"status": "error", "error": str(e)}

    def _trigger_page_modified(self, payload: Dict) -> Dict:
        """[P1-7] 页面修改事件：触发新鲜度检查、演化追踪和自动刷新。"""
        from core.config import get_config

        page_path = payload.get("page_path", "")
        wiki_base = payload.get("wiki_base") or str(get_config().wiki_dir)

        alerts = []
        refresh_result = None
        try:
            from core.kia.proteus import KnowledgeFreshnessChecker
            from core.app.freshness_refresh_worker import FreshnessRefreshWorker
            from core.frontmatter import parse_frontmatter

            full_path = Path(wiki_base).expanduser() / page_path
            fm = {}
            if full_path.exists():
                content = full_path.read_text(encoding="utf-8")
                parsed_fm, _ = parse_frontmatter(content)
                if parsed_fm is not None:
                    fm = parsed_fm

            checker = KnowledgeFreshnessChecker()
            alert = checker.check({"frontmatter": fm, "path": str(full_path)})
            if alert:
                alerts.append(alert)

            # 若配置启用且页面非 timeless，自动刷新日期
            cfg = get_config()
            if alert and cfg.get("daemon.services.freshness_refresh", True):
                temporal_scope = (fm.get("temporal_scope") or fm.get("时效性") or "").strip()
                if temporal_scope not in ("timeless", "永久"):
                    worker = FreshnessRefreshWorker(
                        wiki_base=wiki_base,
                        material_action_resolver=self._trusted_markdown_action_resolver,
                    )
                    refresh_result = worker.refresh_page(
                        str(full_path),
                        material_action_commands=payload.get("material_action_commands"),
                    ).__dict__
        except CHRONOS_OPERATION_ERRORS as e:
            logger.debug("[Chronos] 页面新鲜度检查/刷新失败 %s: %s", page_path, e)

        result = {
            "status": "ok",
            "event_type": "page.modified",
            "page_path": page_path,
            "freshness_alerts": alerts,
        }
        if refresh_result:
            result["freshness_refresh"] = refresh_result
        return result

    # ----------------------------------------------------------
    # 原有任务调度/提醒功能（完整保留）
    # ----------------------------------------------------------

    def schedule(
        self,
        task_type: str,
        subtype: str,
        due_date: datetime,
        context: str = "",
        is_periodic: bool = False,
        period: Optional[str] = None,
        priority: int = 0,
        *,
        material_action_commands: Mapping[str, str] | None = None,
    ) -> str:
        binding = scheduled_task_material_action_binding(
            task_type=task_type,
            subtype=subtype,
            due_date=due_date,
            context=context,
            is_periodic=is_periodic,
            period=period,
            priority=priority,
        )
        authorization = self._resolve_material_action(
            binding,
            material_action_commands,
            action_type=CHRONOS_TASK_CREATE_ACTION,
            source_facts={
                "schema_version": "mnemos.chronos_task_decision_facts.v1",
                "task_type": str(task_type),
                "subtype": str(subtype),
                "due_date": due_date.isoformat(),
                "context": str(context),
                "is_periodic": bool(is_periodic),
                "period": str(period or ""),
                "priority": int(priority),
            },
            task=f"Create scheduled task {task_type}/{subtype}",
            goal="Persist only the exact reminder task requested through Chronos.",
            approved_candidate_key="create_exact_requested_schedule",
            approved_candidate_summary=(
                "Create the exact non-executable reminder task requested by the caller."
            ),
            rejected_candidate_key="reject_drifted_schedule_request",
            rejected_candidate_summary=(
                "Reject a schedule whose type, due date, context, period, or priority drifted."
            ),
            committed_metric="chronos_task_create_receipt",
            rejected_metric="drifted_chronos_task_count",
        )
        task_id = self._build_task_id(
            task_type,
            subtype,
            due_date,
            context,
            identity_hash=binding["input_hash"],
        )
        reminder_date = self._reminder_date_for(due_date)
        expected_task = {
            "task_id": task_id,
            "task_type": task_type,
            "subtype": subtype,
            "due_date": due_date.isoformat(),
            "reminder_date": reminder_date.isoformat(),
            "is_periodic": 1 if is_periodic else 0,
            "period": period,
            "status": "pending",
            "context": context,
            "priority": priority,
        }
        with sqlite3.connect(str(self.DB_PATH), timeout=10) as conn:
            existing = conn.execute(
                "SELECT 1 FROM knowledge_scheduled_tasks WHERE task_id=?",
                (task_id,),
            ).fetchone()
        if existing is not None:
            oracle = ScheduledTaskEffectOracle(
                self.DB_PATH,
                task_id=task_id,
                expected=expected_task,
            )
            recovered = authorization.recover(oracle)
            if recovered is None:
                raise RuntimeError("scheduled-task replay could not observe its existing effect")
            if oracle.observe(authorization.permit) is None:
                raise RuntimeError("terminal scheduled-task receipt lacks exact target evidence")
            return task_id
        permit = require_material_action(
            authorization,
            owner=CHRONOS_OWNER,
            executor_id=CHRONOS_EXECUTOR,
            action_type=CHRONOS_TASK_CREATE_ACTION,
            target_ref=binding["target_ref"],
            input_hash=binding["input_hash"],
            expected_state_db=self.DB_PATH.parent / "producer_consumer_ledger.db",
        )
        before_hash = self._scheduled_task_effect_hash(task_id)
        with sqlite3.connect(str(self.DB_PATH), timeout=10) as conn:
            self._insert_task(
                conn, task_id, task_type, subtype, due_date, context, is_periodic, period, priority
            )
        after_hash = self._scheduled_task_effect_hash(task_id)
        authorization.record_terminal(
            MaterialActionTerminal(
                status="committed",
                target_effect_id=permit.effect_id,
                before_hash=before_hash,
                after_hash=after_hash,
                evidence_refs=(
                    f"material-command:{permit.command_id}",
                    f"decision-revision:{permit.decision_revision_id}",
                    f"material-effect:{permit.effect_id}",
                    f"target-after:{after_hash}",
                    f"target-journal:chronos-task:{task_id}:{after_hash}",
                ),
                outcome="scheduled task created",
                created_at=datetime.now().astimezone().isoformat(),
            )
        )
        return task_id

    def get_pending_reminders(self) -> List[ScheduledTask]:
        now = datetime.now().isoformat()
        with sqlite3.connect(str(self.DB_PATH), timeout=10) as conn:
            cursor = conn.execute(
                """
                SELECT task_id, task_type, subtype, due_date, reminder_date,
                       is_periodic, period, status, context, created_at, reminded_at, priority
                FROM knowledge_scheduled_tasks
                WHERE status = 'pending'
                  AND reminder_date <= ?
                ORDER BY priority DESC, reminder_date ASC
            """,
                (now,),
            )
            return [self._row_to_task(row) for row in cursor.fetchall()]

    def mark_reminded(self, task_id: str):
        with sqlite3.connect(str(self.DB_PATH), timeout=10) as conn:
            conn.execute(
                """
                UPDATE knowledge_scheduled_tasks
                SET status = 'reminded', reminded_at = ?
                WHERE task_id = ?
            """,
                (datetime.now().isoformat(), task_id),
            )

    def mark_completed(self, task_id: str):
        with sqlite3.connect(str(self.DB_PATH), timeout=10) as conn:
            row = conn.execute(
                """
                SELECT task_type, subtype, due_date, is_periodic, period, context, priority
                FROM knowledge_scheduled_tasks
                WHERE task_id = ?
            """,
                (task_id,),
            ).fetchone()
            conn.execute(
                """
                UPDATE knowledge_scheduled_tasks
                SET status = 'completed', completed_at = ?
                WHERE task_id = ?
            """,
                (datetime.now().isoformat(), task_id),
            )
            if row and row[3]:
                next_due = self._next_periodic_due(datetime.fromisoformat(row[2]), row[4])
                if next_due:
                    next_id = self._build_task_id(row[0], row[1], next_due, row[5] or "")
                    self._insert_task(
                        conn,
                        next_id,
                        row[0],
                        row[1],
                        next_due,
                        context=row[5] or "",
                        is_periodic=True,
                        period=row[4],
                        priority=row[6] or 0,
                    )

    def cancel(self, task_id: str):
        with sqlite3.connect(str(self.DB_PATH), timeout=10) as conn:
            conn.execute(
                """
                UPDATE knowledge_scheduled_tasks
                SET status = 'cancelled'
                WHERE task_id = ?
            """,
                (task_id,),
            )

    def startup_compensation(self, max_tasks: Optional[int] = None) -> List[ScheduledTask]:
        """返回停机期间错过的任务，数量受 *max_tasks* 限制。"""
        max_tasks = max_tasks if max_tasks is not None else self.MAX_STARTUP_COMPENSATION_TASKS
        now = datetime.now().isoformat()
        missed = []  # type: ignore[var-annotated]

        with sqlite3.connect(str(self.DB_PATH), timeout=10) as conn:
            cursor = conn.execute(
                """
                SELECT task_id, task_type, subtype, due_date, reminder_date,
                       is_periodic, period, status, context, created_at, reminded_at, priority
                FROM knowledge_scheduled_tasks
                WHERE status = 'pending'
                  AND reminder_date <= ?
                ORDER BY priority DESC, reminder_date ASC
                LIMIT ?
            """,
                (now, max_tasks),
            )
            missed.extend(self._row_to_task(row) for row in cursor.fetchall())

            three_days_ago = (datetime.now() - timedelta(days=3)).isoformat()
            cursor = conn.execute(
                """
                SELECT task_id, task_type, subtype, due_date, reminder_date,
                       is_periodic, period, status, context, created_at, reminded_at, priority
                FROM knowledge_scheduled_tasks
                WHERE status = 'reminded'
                  AND reminded_at <= ?
                ORDER BY priority DESC, reminded_at ASC
                LIMIT ?
            """,
                (three_days_ago, max_tasks),
            )
            missed.extend(self._row_to_task(row) for row in cursor.fetchall())

        # 合并后按优先级截断，确保总数量不超过限制
        missed.sort(key=lambda t: (-t.priority, t.reminder_date or t.due_date or ""))
        return missed[:max_tasks]

    def format_reminder(self, task: ScheduledTask) -> str:
        due = datetime.fromisoformat(task.due_date.replace("Z", "+00:00"))
        days_until = (due - datetime.now()).days
        lines = [
            "**任务提醒**",
            "",
            f"类型：{task.task_type}/{task.subtype}",
            f"执行日期：{task.due_date[:10]}（还有 {days_until} 天）",
        ]
        if task.is_periodic:
            lines.append(f"周期：{task.period}")
        if task.context:
            lines.append(f"上下文：{task.context}")
        lines.append("")
        lines.append("知识库已装载相关经验，请查看。")
        return "\n".join(lines)

    def list_all(self, status: Optional[str] = None) -> List[ScheduledTask]:
        with sqlite3.connect(str(self.DB_PATH), timeout=10) as conn:
            if status:
                cursor = conn.execute(
                    """
                    SELECT task_id, task_type, subtype, due_date, reminder_date,
                           is_periodic, period, status, context, created_at, reminded_at, priority
                    FROM knowledge_scheduled_tasks
                    WHERE status = ?
                    ORDER BY priority DESC, due_date ASC
                """,
                    (status,),
                )
            else:
                cursor = conn.execute("""
                    SELECT task_id, task_type, subtype, due_date, reminder_date,
                           is_periodic, period, status, context, created_at, reminded_at, priority
                    FROM knowledge_scheduled_tasks
                    ORDER BY priority DESC, due_date ASC
                """)
            return [self._row_to_task(row) for row in cursor.fetchall()]

    def cleanup_old_tasks(self, days: int = KNOWLEDGE_SCHEDULER_CLEANUP_OLD_TASKS_DAYS):
        cutoff = (datetime.now() - timedelta(days=days)).isoformat()
        with sqlite3.connect(str(self.DB_PATH), timeout=10) as conn:
            conn.execute(
                """
                DELETE FROM knowledge_scheduled_tasks
                WHERE status IN ('completed', 'cancelled')
                  AND completed_at <= ?
            """,
                (cutoff,),
            )

    def _row_to_task(self, row) -> ScheduledTask:
        return ScheduledTask(
            task_id=row[0],
            task_type=row[1],
            subtype=row[2],
            due_date=row[3],
            reminder_date=row[4],
            is_periodic=bool(row[5]),
            period=row[6],
            status=row[KNOWLEDGE_SCHEDULER__ROW_TO_TASK_ROW],
            context=row[8],
            created_at=row[9],
            reminded_at=row[10],
            priority=row[11] if len(row) > 11 else 0,
        )

    @staticmethod
    def _reminder_date_for(due_date: datetime) -> datetime:
        days_until = (due_date - datetime.now()).days
        if days_until <= KNOWLEDGE_SCHEDULER__REMINDER_DATE_FOR_DAYS_UNTIL_DAYS:
            reminder_days = 1
        elif days_until <= KNOWLEDGE_SCHEDULER__REMINDER_DATE_FOR_DAYS_UNTIL_DAYS_2:
            reminder_days = 3
        else:
            reminder_days = REMINDER_DAYS
        return due_date - timedelta(days=reminder_days)

    def _insert_task(
        self,
        conn: sqlite3.Connection,
        task_id: str,
        task_type: str,
        subtype: str,
        due_date: datetime,
        context: str = "",
        is_periodic: bool = False,
        period: Optional[str] = None,
        priority: int = 0,
    ):
        reminder_date = self._reminder_date_for(due_date)
        conn.execute(
            """
            INSERT INTO knowledge_scheduled_tasks
            (task_id, task_type, subtype, due_date, reminder_date,
             is_periodic, period, status, context, priority, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?, ?)
        """,
            (
                task_id,
                task_type,
                subtype,
                due_date.isoformat(),
                reminder_date.isoformat(),
                1 if is_periodic else 0,
                period,
                context,
                priority,
                datetime.now().isoformat(),
            ),
        )

    @staticmethod
    def _build_task_id(
        task_type: str,
        subtype: str,
        due_date: datetime,
        context: str = "",
        *,
        identity_hash: str = "",
    ) -> str:
        base = f"{task_type}-{subtype}-{due_date.strftime('%Y%m%d')}"
        if identity_hash.startswith("sha256:"):
            suffix = identity_hash.split(":", 1)[1][:8]
        else:
            suffix_seed = f"{base}-{context}-{datetime.now().isoformat()}"
            suffix = hashlib.md5(
                suffix_seed.encode("utf-8"),
                usedforsecurity=False,
            ).hexdigest()[:8]
        return f"{base}-{suffix}"

    @staticmethod
    def _next_periodic_due(due_date: datetime, period: Optional[str]) -> Optional[datetime]:
        if period == "daily":
            return due_date + timedelta(days=1)
        if period == "weekly":
            return due_date + timedelta(days=KNOWLEDGE_SCHEDULER_DURATION_BUCKET_WEEK_DAYS)
        if period == "biweekly":
            return due_date + timedelta(days=14)
        if period == "monthly":
            return due_date + timedelta(days=KNOWLEDGE_SCHEDULER_DURATION_BUCKET_MONTH_DAYS)
        if period == "quarterly":
            return due_date + timedelta(days=KNOWLEDGE_SCHEDULER_DURATION_BUCKET_QUARTER_DAYS)
        if period == "yearly":
            return due_date + timedelta(days=KNOWLEDGE_SCHEDULER_DURATION_BUCKET_YEAR_DAYS)
        return None


# ============================================================
# 便捷函数
# ============================================================


def schedule_task(
    task_type: str,
    subtype: str,
    due_date: datetime,
    context: str = "",
    is_periodic: bool = False,
    period: Optional[str] = None,
    priority: int = 0,
    *,
    material_action_commands: Mapping[str, str] | None = None,
) -> str:
    scheduler = KnowledgeScheduler()
    return scheduler.schedule(
        task_type,
        subtype,
        due_date,
        context,
        is_periodic,
        period,
        priority,
        material_action_commands=material_action_commands,
    )


def check_reminders() -> List[ScheduledTask]:
    scheduler = KnowledgeScheduler()
    return scheduler.get_pending_reminders()
