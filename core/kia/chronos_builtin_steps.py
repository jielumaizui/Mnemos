"""Built-in Chronos registrations and bounded task adapters."""

from __future__ import annotations

import logging
from pathlib import Path
import sqlite3
import time
from typing import TYPE_CHECKING, cast, Dict

from core.kia.chronos_contracts import (
    CHRONOS_OPERATION_ERRORS,
    CronTrigger,
    KNOWLEDGE_SCHEDULER_DURATION_BUCKET_MONTH_DAYS,
    PassiveTrigger,
    ScheduledStep,
    STATS_DAYS,
    TIMEOUT_SECONDS_2,
)
from daemon.training_governance_service import run_service as run_training_governance_service

logger = logging.getLogger(__name__)


class ChronosBuiltinStepMixin:
    """Register and execute the scheduler's built-in bounded adapters."""

    if TYPE_CHECKING:
        steps: Dict[str, ScheduledStep]

        def cleanup_old_tasks(self, days: int = ...) -> int: ...

    def _register_event_handlers(self):
        """[P1-7] 将 Chronos trigger_event 注册为 EventBus 消费者。"""
        if self._event_handlers_registered:
            return
        try:
            from core.mnemos_bus import get_event_bus

            bus = get_event_bus()
            for step_name, event_type in self._event_trigger_routes():

                def _make_handler(name, et):
                    def _handler(event):
                        step = self.steps.get(name)
                        if step is not None and not step.enabled:
                            return {
                                "status": "skipped",
                                "reason": "disabled",
                                "event_type": et,
                            }
                        return self.trigger_event(
                            et, event.payload if hasattr(event, "payload") else {}
                        )

                    return _handler

                handler = _make_handler(step_name, event_type)
                bus.subscribe(event_type, handler, consumer_id=f"chronos:{step_name}")
                logger.info("[Chronos] 已订阅事件: %s", event_type)
            self._event_handlers_registered = True
        except CHRONOS_OPERATION_ERRORS as e:
            logger.warning("[Chronos] EventBus 订阅失败: %s", e, exc_info=True)

        # --- 被动调用步骤 ---
        self.register(
            ScheduledStep(
                name="time_parser",
                func=lambda: {"status": "passive"},
                trigger=PassiveTrigger(),
            )
        )

        # --- 调度中心自身 ---
        self.register(
            ScheduledStep(
                name="knowledge_sched",
                func=self._run_sched_maintenance,
                trigger=CronTrigger("*/5 * * * *"),
                timeout=60,
            )
        )

        # --- 强制复盘检查 ---
        self.register(
            ScheduledStep(
                name="forced_retrospective",
                func=self._run_forced_retrospective,
                trigger=CronTrigger("*/30 * * * *"),
                timeout=TIMEOUT_SECONDS_2,
            )
        )

        # --- ScorerV2 训练队列检查（每小时，数据积累阶段） ---
        self.register(
            ScheduledStep(
                name="scorer_training",
                func=self._run_scorer_training,
                trigger=CronTrigger("0 21 * * *"),  # 每天 21:00
                timeout=60,
            )
        )

        # --- 问题处理流水线（每天扫描自动修复） ---
        self.register(
            ScheduledStep(
                name="issue_pipeline",
                func=self._run_issue_pipeline,
                trigger=CronTrigger("30 15 * * *"),  # 每天 15:30
                timeout=300,
            )
        )

        # --- 页面横幅任务扫描（处理用户打勾选项） ---
        self.register(
            ScheduledStep(
                name="banner_task_scanner",
                func=self._run_banner_task_scanner,
                trigger=CronTrigger("*/30 * * * *"),  # 每 30 分钟
                timeout=120,
            )
        )

        # --- 对话提醒清理（每天清理过期记录） ---
        self.register(
            ScheduledStep(
                name="dialog_reminder_cleanup",
                func=self._run_dialog_reminder_cleanup,
                trigger=CronTrigger("0 16 * * *"),  # 每天 16:00
                timeout=60,
            )
        )

        # --- 画像周报生成（每周一 9:00） ---
        self.register(
            ScheduledStep(
                name="weekly_report",
                func=lambda: self._run_weekly_report(),
                trigger=CronTrigger("30 10 * * 1"),  # 每周一 10:30
                timeout=300,
            )
        )
        self.register(
            ScheduledStep(
                name="raw_survival_refresh",
                func=self._run_raw_survival_refresh,
                trigger=CronTrigger("10 10 * * 1"),  # 每周一 10:10；错过则下次 tick 补跑
                timeout=300,
            )
        )
        self.register(
            ScheduledStep(
                name="verification_queue",
                func=self._run_verification_queue,
                trigger=CronTrigger(self._verification_queue_cron()),
                timeout=120,
            )
        )

    def _flywheel_predicate(self) -> bool:
        """认知决策飞轮条件：画像信号数 >= 50。"""
        try:
            from core.persona.psyche import get_signal_store

            stats = get_signal_store().get_signal_stats(days=STATS_DAYS)
            total = sum(v for v in stats.values() if v > 0)
            return total >= 50  # type: ignore[no-any-return]
        except (
            OSError,
            ValueError,
            TypeError,
            KeyError,
            ImportError,
            AttributeError,
            RuntimeError,
            sqlite3.Error,
        ):
            logger.warning("cognitive_decision_flywheel predicate 画像信号统计失败", exc_info=True)
            return False

    def _run_kia_module(
        self, module_name: str, class_name: str, method_name: str, wiki_base: str | None = None
    ) -> Dict:
        """通用 KIA 模块执行器"""
        try:
            import importlib

            from core.import_guard import assert_allowed_module

            full_module_name = f"core.kia.{module_name}"
            assert_allowed_module(full_module_name)
            mod = importlib.import_module(full_module_name)
            cls = getattr(mod, class_name)
            instance = cls(wiki_base=wiki_base)
            method = getattr(instance, method_name)
            result = method()
            if isinstance(result, dict):
                return result
            return {"status": "ok", "result": str(result)}
        except CHRONOS_OPERATION_ERRORS as e:
            logger.error("KIA 模块执行失败 %s.%s.%s: %s", module_name, class_name, method_name, e)
            return {"status": "error", "error": str(e)}

    def _run_knowledge_profile(self, wiki_base: str) -> Dict:
        """运行 Metis 知识画像报告入口。"""
        try:
            from core.kia.metis import generate_profile

            result = generate_profile(wiki_base=wiki_base)
            if isinstance(result, dict):
                return result
            return {"status": "ok", "result": str(result)}
        except CHRONOS_OPERATION_ERRORS as e:
            logger.error("Metis 知识画像执行失败: %s", e)
            return {"status": "error", "error": str(e)}

    def _run_skill_flywheel(self, wiki_base: str) -> Dict:
        """运行认知决策飞轮；函数名保留到问题 25 做兼容迁移。"""
        try:
            from core.kia.ixion import CognitiveDecisionFlywheel

            flywheel = CognitiveDecisionFlywheel(wiki_base=wiki_base)
            results = flywheel.run_cycle()
            count_keys = (
                "wiki_to_cognitive_decision skill_to_cognitive_decision "
                "behavior_to_cognitive_decision"
            ).split()
            counts = {key: len(results.get(key, [])) for key in count_keys}
            return {
                "status": "ok",
                **counts,
                "executed": results.get("executed", {}).get("count", 0),
                "report_path": results.get("report_path", ""),
            }
        except CHRONOS_OPERATION_ERRORS as e:
            logger.error("认知决策飞轮执行失败: %s", e, exc_info=True)
            return {"status": "error", "error": str(e)}

    def _run_graph_build(self, wiki_base: str) -> Dict:
        """知识图谱关系构建：遍历 wiki 页面发现新关系"""
        try:
            from core.kia.knowledge_graph import KnowledgeGraph

            kg = KnowledgeGraph(wiki_base=wiki_base)
            wiki_path = Path(wiki_base)
            pages = []
            for p in wiki_path.rglob("*.md"):
                rel = p.relative_to(wiki_path)
                if any(part.startswith(".") for part in rel.parts):
                    continue
                if p.name.endswith(".shadow.md"):
                    continue
                pages.append(p)
            added = 0
            for page in pages[:100]:
                try:
                    for rel in kg.discover_relations(page):  # type: ignore[assignment]
                        if kg.add_relation(rel):  # type: ignore[arg-type]
                            added += 1
                except (
                    OSError,
                    ValueError,
                    TypeError,
                    KeyError,
                    ImportError,
                    AttributeError,
                    RuntimeError,
                    sqlite3.Error,
                ):
                    logger.warning("图谱关系发现失败: %s", page.name)
                    continue
            return {"status": "ok", "relations_added": added}
        except CHRONOS_OPERATION_ERRORS as e:
            logger.error("图谱构建失败: %s", e)
            return {"status": "error", "error": str(e)}

    def _run_heat_map(self, wiki_base: str) -> Dict:
        """热力地图：衰减热力分数 + 反写 frontmatter + 生成报告"""
        try:
            from core.wiki_metrics import WikiMetrics

            wm = WikiMetrics(wiki_dir=wiki_base)

            # 1. 全局热力衰减
            decayed = wm.decay_all()

            # 2. 反写 frontmatter 到所有页面
            wiki_path = Path(wiki_base)
            synced = 0
            for p in wiki_path.rglob("*.md"):
                rel = p.relative_to(wiki_path)
                if any(part.startswith(".") for part in rel.parts):
                    continue
                if p.name.endswith(".shadow.md"):
                    continue
                try:
                    if wm.sync_heat_to_frontmatter(p):
                        synced += 1
                except (
                    OSError,
                    ValueError,
                    TypeError,
                    KeyError,
                    ImportError,
                    AttributeError,
                    RuntimeError,
                    sqlite3.Error,
                ):
                    logger.warning("热力 frontmatter 同步失败: %s", p.name)
                    continue

            # 3. 生成热力地图报告
            report = wm.generate_heat_report(write=True, wiki_dir=wiki_base)

            return {
                "status": "ok",
                "decayed": decayed,
                "frontmatter_synced": synced,
                "report_length": len(report),
            }
        except CHRONOS_OPERATION_ERRORS as e:
            logger.error("热力地图失败: %s", e)
            return {"status": "error", "error": str(e)}

    def _run_sched_maintenance(self) -> Dict:
        """调度器自身维护：清理过期任务、检查步骤健康"""
        self.cleanup_old_tasks()
        return {"status": "ok", "steps_registered": len(self.steps)}

    def _run_forced_retrospective(self) -> Dict:
        """强制复盘检查：到期预约直接打开 Obsidian，系统提醒走权重"""
        try:
            from core.app.forced_retrospective import ForcedRetrospective

            fr = ForcedRetrospective()
            decisions = fr.check_due_reminders()
            forced = sum(1 for d in decisions if d.should_force_open)
            reminded = sum(1 for d in decisions if not d.should_force_open)
            return {
                "status": "ok",
                "forced_open": forced,
                "dialog_reminder": reminded,
            }
        except CHRONOS_OPERATION_ERRORS as e:
            logger.error("强制复盘检查失败: %s", e)
            return {"status": "error", "error": str(e)}

    def _run_scorer_training(self) -> Dict:
        """Delegate the retired queue tick to canonical training governance."""

        return run_training_governance_service(
            lambda service, exc: logger.error(
                "%s failed: %s",
                service,
                exc,
            )
        )

    def _run_issue_pipeline(self, registry=None) -> Dict:
        """问题处理流水线：自动修复低风险问题，并为高风险问题创建争议页。"""
        try:
            from core.kia.issue_pipeline import (
                IssueRegistry,
                get_auto_fix_executor,
                get_dispute_generator,
            )

            if registry is None:
                registry = IssueRegistry()
            executor = get_auto_fix_executor(registry=registry)
            pending = registry.list_issues(status="detected", limit=100)
            auto_fixable = []
            manual_review = []
            for issue in pending:
                if executor.can_auto_fix(issue):
                    auto_fixable.append(issue)
                elif issue.severity in ("critical", "high"):
                    manual_review.append(issue)

            results = []
            for issue in auto_fixable:
                result = executor.execute(issue)
                results.append(
                    {
                        "issue_id": issue.issue_id,
                        "type": issue.issue_type,
                        "success": result.success,
                        "skipped": result.skipped,
                        "action": result.action,
                    }
                )
            disputes = []
            if manual_review:
                dispute_generator = get_dispute_generator()
                for issue in manual_review:
                    page_path = dispute_generator.generate(issue)
                    page_path_text = str(page_path)
                    registry.update_status(
                        issue.issue_id,
                        "dispute",
                        resolved_by="issue_pipeline",
                        resolution_action="created_dispute_page",
                        resolution_notes=page_path_text,
                    )
                    disputes.append(
                        {
                            "issue_id": issue.issue_id,
                            "type": issue.issue_type,
                            "page_path": page_path_text,
                        }
                    )
            return {
                "status": "ok",
                "scanned": len(pending),
                "auto_fixable": len(auto_fixable),
                "disputes_created": len(disputes),
                "severity_counts": registry.count_by_severity(),
                "results": results,
                "disputes": disputes,
            }
        except CHRONOS_OPERATION_ERRORS as e:
            logger.error("问题处理流水线失败: %s", e)
            return {"status": "error", "error": str(e)}

    def _run_banner_task_scanner(self) -> Dict:
        """页面横幅任务扫描：处理用户在 Obsidian 中打勾的选项"""
        try:
            from core.kia.dialog_reminder import (
                get_dialog_reminder_queue,
                get_page_banner_injector,
            )

            injector = get_page_banner_injector()
            queue = get_dialog_reminder_queue()
            stats = injector.process_banners(queue=queue)
            return {
                "status": "ok",
                "stats": stats,
            }
        except CHRONOS_OPERATION_ERRORS as e:
            logger.error("页面横幅任务扫描失败: %s", e)
            return {"status": "error", "error": str(e)}

    def _run_dialog_reminder_cleanup(self, queue=None) -> Dict:
        """对话提醒清理：删除已解决/已忽略超过 30 天的旧记录"""
        try:
            from core.kia.dialog_reminder import DialogReminderQueue

            if queue is None:
                queue = DialogReminderQueue()
            deleted = queue.cleanup_resolved(
                retention_days=KNOWLEDGE_SCHEDULER_DURATION_BUCKET_MONTH_DAYS
            )
            stats = queue.count_by_status()
            return {
                "status": "ok",
                "deleted": deleted,
                "current_stats": stats,
            }
        except CHRONOS_OPERATION_ERRORS as e:
            logger.error("对话提醒清理失败: %s", e)
            return {"status": "error", "error": str(e)}

    def _run_weekly_report(self, wiki_base: str | None = None) -> Dict:
        """画像周报生成"""
        try:
            from core.app.weekly_report import WeeklyReportGenerator

            gen = WeeklyReportGenerator(wiki_base=wiki_base)
            content = gen.generate_weekly_report()
            return {"status": "ok", "content_length": len(content)}
        except CHRONOS_OPERATION_ERRORS as e:
            logger.error("周报生成失败: %s", e)
            return {"status": "error", "error": str(e)}

    def _run_raw_survival_refresh(self) -> Dict:
        """刷新 raw 生存值和热窗口投影清理状态。"""
        try:
            from core.config import get_config
            from core.sync_framework.raw_event_store import RawEventStore

            cfg = get_config()
            startup_delay = int(cfg.get("raw_event_store.startup_delay_seconds", 600))
            if startup_delay > 0:
                try:
                    import psutil

                    uptime = time.time() - psutil.boot_time()
                    if uptime < startup_delay:
                        return {
                            "status": "deferred",
                            "reason": "startup_delay",
                            "retry_after_seconds": int(startup_delay - uptime),
                        }
                except ImportError:
                    logger.debug("psutil 不可用，跳过 raw 生存值启动延迟检查")
                except (OSError, psutil.Error):
                    logger.debug("raw 生存值刷新启动延迟检查失败，继续执行", exc_info=True)

            store = RawEventStore()
            try:
                summary = store.refresh_survival_scores()
                purge_summary = {
                    "purged": 0,
                    "raw_turns_deleted": 0,
                    "raw_metrics_deleted": 0,
                    "raw_access_logs_deleted": 0,
                }
                if bool(cfg.get("raw_event_store.physical_delete_enabled", True)):
                    purge_summary = store.purge_eligible_delete(
                        limit=int(cfg.get("raw_event_store.physical_delete_batch_limit", 10000))
                    )
            finally:
                store.close()
            return {"status": "ok", **summary, "physical_purge": purge_summary}
        except CHRONOS_OPERATION_ERRORS as e:
            logger.error("raw 生存值刷新失败: %s", e)
            return {"status": "error", "error": str(e)}

    def _verification_queue_cron(self) -> str:
        try:
            from core.config import get_config

            return str(get_config().get("verification_queue.cron", "20 16 * * *"))
        except CHRONOS_OPERATION_ERRORS:
            logger.debug("读取 verification_queue cron 失败，使用默认值", exc_info=True)
            return "20 16 * * *"

    def _run_verification_queue(self) -> Dict:
        """规划受控求证队列；默认只写 verification DB/report，不改 Wiki 正文。"""
        try:
            from core.cognitive.verification_queue import run_verification_queue

            return cast(Dict, run_verification_queue(apply=True, background=True))
        except CHRONOS_OPERATION_ERRORS as e:
            logger.error("受控求证队列执行失败: %s", e)
            return {"status": "error", "error": str(e)}
