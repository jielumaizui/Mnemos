# -*- coding: utf-8 -*-
"""Maintenance and status helpers for the daemon."""

from __future__ import annotations

import logging
import platform
import sqlite3
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Dict, List


logger = logging.getLogger(__name__)

MAINTENANCE_OPERATION_ERRORS = (
    ImportError,
    KeyError,
    OSError,
    RuntimeError,
    TypeError,
    ValueError,
    sqlite3.Error,
)


def run_startup_compensation(
    log_service_error: Callable[[str, Exception], None],
) -> Dict[str, Any]:
    result = {"compensated": 0}
    try:
        from core.app.forced_retrospective import ForcedRetrospective

        scheduler = ForcedRetrospective()
        missed = scheduler.startup_compensation()
        result["compensated"] = len(missed)
    except MAINTENANCE_OPERATION_ERRORS as exc:
        log_service_error("startup_compensation", exc)
    return result


def format_model_status(
    *,
    count_daemon_processes: Callable[[], int],
    daemon_pid: int,
    now_func: Callable[[], datetime] = datetime.now,
    platform_func: Callable[[], str] = platform.system,
) -> str:
    lines = [f"Mnemos Daemon Status @ {now_func().isoformat()}"]
    lines.append(f"  PID: {daemon_pid}")
    lines.append(f"  Platform: {platform_func()}")
    lines.append(f"  Daemon processes: {count_daemon_processes()}")
    return "\n".join(lines)


def generate_drift_report(
    log_service_error: Callable[[str, Exception], None],
    *,
    log_info: Callable[..., None] | None = None,
    now_func: Callable[[], datetime] = datetime.now,
) -> Dict[str, Any]:
    result: Dict[str, Any] = {
        "drifts": [],
        "generated_at": now_func().isoformat(),
        "profile_version": None,
        "previous_version": None,
        "drift_count": 0,
    }
    try:
        from core.persona.delphi import PersonaStore
        from core.persona.pythia import PreferenceAnalyzer

        # Use persisted persona versions to avoid marking signals processed
        # without saving the newly analyzed profile.
        persona_store = PersonaStore()
        recent = persona_store.load_recent_personas(limit=2)
        if len(recent) < 2:
            result["note"] = "画像历史版本不足，跳过漂移检测"
            return result

        current, previous = recent[0], recent[1]
        if current.signal_count == 0 or previous.signal_count == 0:
            result["note"] = "画像信号数为空，跳过漂移检测"
            return result

        analyzer = PreferenceAnalyzer()
        drifts = analyzer.detect_drift(current, previous)

        result["drifts"] = drifts
        result["profile_version"] = current.version
        result["previous_version"] = previous.version
        result["drift_count"] = len(drifts)
        if drifts and log_info is not None:
            log_info("[DAEMON] 画像漂移报告: %d 条警报", len(drifts))
    except MAINTENANCE_OPERATION_ERRORS as exc:
        log_service_error("drift_report", exc)
    return result


def run_preflight_checks(
    *,
    now_func: Callable[[], datetime] = datetime.now,
) -> Dict[str, Any]:
    checks: Dict[str, Any] = {"timestamp": now_func().isoformat()}
    try:
        from core.config import get_config

        cfg = get_config()
        checks["wiki_dir_exists"] = cfg.wiki_dir.exists()
        checks["data_dir_exists"] = cfg.data_dir.exists()
        checks["database_dir_exists"] = cfg.database_dir.exists()
        checks["config_ok"] = True
    except MAINTENANCE_OPERATION_ERRORS as exc:
        checks["config_error"] = str(exc)
        checks["config_ok"] = False
    return checks


def build_push_context(
    user_message: str = "",
    *,
    now_func: Callable[[], datetime] = datetime.now,
) -> Dict[str, Any]:
    return {
        "user_message": user_message,
        "timestamp": now_func().isoformat(),
        "recommendations": [],
    }


def run_startup_cleanup(
    log_service_error: Callable[[str, Exception], None],
) -> Dict[str, Any]:
    """
    Daemon 启动时执行的一次性清理，防止历史数据目录无限增长。

    清理范围：
    - distill_failed/ 过期文件
    - distill_messages/ 已完成/失败/归档任务的旧消息文件
    - capture_artifacts/ 过期/过大目录
    """
    from pathlib import Path

    from core.config import get_config

    result: Dict[str, Any] = {
        "distill_failed": {"removed": 0, "remaining": 0},
        "distill_messages": {"archived": 0},
        "capture_artifacts": {"removed_dirs": 0, "removed_bytes": 0},
    }

    try:
        cfg = get_config()
        database_dir = Path(cfg.database_dir)
    except MAINTENANCE_OPERATION_ERRORS as exc:
        log_service_error("startup_cleanup_config", exc)
        return result

    # 1. 清理 distill_failed
    try:
        from core.hephaestus.distillation_failure import cleanup_failed_distill

        result["distill_failed"] = cleanup_failed_distill(
            database_dir,
            ttl_days=30,
            max_count=1000,
        )
    except MAINTENANCE_OPERATION_ERRORS as exc:
        log_service_error("startup_cleanup_distill_failed", exc)

    # 2. 清理 distill_messages
    try:
        from core.kia import amphora

        result["distill_messages"]["archived"] = amphora.cleanup_old(days=7)
    except MAINTENANCE_OPERATION_ERRORS as exc:
        log_service_error("startup_cleanup_distill_messages", exc)

    # 3. Capture retention is a separate, receipt-backed maintenance path.
    # It must not reconstruct CaptureService: that would turn startup cleanup
    # into a producer/schema side effect and could make a diagnostic path
    # mutate the queue.
    try:
        from core.sync_framework.capture_maintenance import CaptureRetentionMaintenance

        maintenance = CaptureRetentionMaintenance(config=cfg)
        plan = maintenance.plan(
            payload_retention_days=int(cfg.get("capture.payload_retention_days", 30)),
            artifact_retention_days=int(cfg.get("capture.artifact_ttl_days", 30)),
            artifact_max_total_bytes=int(
                cfg.get("capture.artifact_max_total_bytes", 1024 * 1024 * 1024)
            ),
        )
        applied = maintenance.apply(plan)
        result["capture_artifacts"] = {
            "removed_dirs": applied["deleted_artifacts"],
            "removed_bytes": applied["deleted_artifact_bytes"],
            "receipt_id": applied["receipt_id"],
            "deleted_payloads": applied["deleted_payloads"],
        }
    except MAINTENANCE_OPERATION_ERRORS as exc:
        log_service_error("startup_cleanup_capture_artifacts", exc)

    return result


class DatabaseMaintenanceTask:
    """数据库定期维护任务：按配置保留期清理过期数据，执行 WAL checkpoint、optimize、VACUUM。"""

    def __init__(self, config: Any | None = None):
        from core.config import get_config

        self.config = config or get_config()
        self._last_run = 0.0

    def _retention_days(self, key: str, default: int = 90) -> int:
        return int(self.config.get(f"storage.retention_days.{key}", default))

    def _db_paths(self) -> Dict[str, Path]:
        from core.runtime_paths import RuntimePaths

        database_dir = Path(str(self.config.database_dir))
        return {
            "observations": database_dir / "observations.db",
            "reflections": database_dir / "reflections.db",
            "user_signals": database_dir / "user_signals.db",
            "application_signals": database_dir / "application_signals.db",
            "wiki_metrics": database_dir / "wiki_metrics.db",
            "mnemos": database_dir / "mnemos.db",
            "link_probe": database_dir / "link_probe.db",
            "model_call_ledger": RuntimePaths.from_config(self.config).model_call_ledger_db,
            "distillation_chunks": database_dir / "distillation_chunks.db",
            "knowledge_graph": database_dir / "knowledge_graph.db",
        }

    def _is_non_sqlite_payload(self, db_path: Path) -> bool:
        """Reject non-SQLite payloads without consulting retired config."""
        if db_path.exists():
            try:
                with open(db_path, "rb") as f:
                    header = f.read(16)
                    if header.startswith(b"{"):
                        return True
            except OSError:
                # 文件不可读时保守地视为非加密，让后续连接自身报错
                pass
        return False

    def _cleanup_stores(self, dry_run: bool = False) -> Dict[str, int]:
        """调用各 Store 的 cleanup_older_than 完成保留期清理。"""
        from core.app.application_signal_service import ApplicationSignalService
        from core.cognitive.observation_store import ObservationStore
        from core.db_utils import delete_older_than
        from core.hephaestus.chunk_checkpoint import ChunkCheckpointStore
        from core.hephaestus.link_probe_worker import LinkProbeWorker
        from core.persona.behavior_tracker import BehaviorPromptTracker
        from core.reflection.reflection_store import ReflectionStore
        from core.scoring.adaptive_scorer_v2 import AdaptiveScorerV2
        from core.telemetry.prompt_call_log import ModelCallLedger
        from core.wiki_metrics import WikiMetrics

        db_paths = self._db_paths()
        stores: List[tuple[str, type, Path, Dict[str, Any], str]] = [
            ("observations", ObservationStore, db_paths["observations"], {}, "observations"),
            ("reflections", ReflectionStore, db_paths["reflections"], {}, "reflections"),
            ("user_signals", BehaviorPromptTracker, db_paths["user_signals"], {}, "user_signals"),
            (
                "application_signals",
                ApplicationSignalService,
                db_paths["application_signals"],
                {"config": self.config},
                "application_signals",
            ),
            ("wiki_metrics_query_log", WikiMetrics, db_paths["wiki_metrics"], {}, "wiki_metrics_query_log"),
            ("mnemos_search_sessions", AdaptiveScorerV2, db_paths["mnemos"], {}, "mnemos_search_sessions"),
            ("link_probe_queue", LinkProbeWorker, db_paths["link_probe"], {}, "link_probe_queue"),
            (
                "model_call_ledger",
                ModelCallLedger,
                db_paths["model_call_ledger"],
                {"config": self.config},
                "model_call_ledger",
            ),
            (
                "distillation_chunks",
                ChunkCheckpointStore,
                db_paths["distillation_chunks"],
                {},
                "distillation_chunks",
            ),
        ]

        deleted: Dict[str, int] = {}
        for name, cls, path, kwargs, retention_key in stores:
            days = self._retention_days(retention_key, 90)
            try:
                store = cls(db_path=path, **kwargs)
                count = store.cleanup_older_than(days, dry_run=dry_run)
                deleted[name] = count
                logger.info("[DBMaintenance] %s %s: %d rows", name, "would_delete" if dry_run else "deleted", count)
            except MAINTENANCE_OPERATION_ERRORS as exc:
                logger.warning("[DBMaintenance] cleanup %s failed: %s", name, exc, exc_info=True)
                deleted[name] = -1

        # knowledge_graph 没有独立 Store.cleanup 方法，直接使用通用 helper 清理 relations。
        try:
            kg_path = db_paths["knowledge_graph"]
            if kg_path.exists():
                days = self._retention_days("knowledge_graph", 365)
                with sqlite3.connect(str(kg_path), timeout=10) as conn:
                    count = delete_older_than(conn, "relations", "updated_at", days, dry_run=dry_run)
                    deleted["knowledge_graph"] = count
                    logger.info(
                        "[DBMaintenance] knowledge_graph %s: %d rows",
                        "would_delete" if dry_run else "deleted",
                        count,
                    )
                from core.kia.kg_consistency import repair_kg_consistency

                consistency = repair_kg_consistency(
                    kg_path,
                    apply=not dry_run,
                    create_backup=False,
                )
                if dry_run:
                    deleted["knowledge_graph_relation_evidence_orphans"] = int(
                        consistency.get("would_delete", {}).get("relation_evidence", 0)
                    )
                    deleted["knowledge_graph_relation_embedding_orphans"] = int(
                        consistency.get("would_delete", {}).get(
                            "relation_context_embeddings", 0
                        )
                    )
                else:
                    deleted["knowledge_graph_relation_evidence_orphans"] = int(
                        consistency.get("deleted", {}).get("relation_evidence", 0)
                    )
                    deleted["knowledge_graph_relation_embedding_orphans"] = int(
                        consistency.get("deleted", {}).get(
                            "relation_context_embeddings", 0
                        )
                    )
        except MAINTENANCE_OPERATION_ERRORS as exc:
            logger.warning("[DBMaintenance] cleanup knowledge_graph failed: %s", exc, exc_info=True)
            deleted["knowledge_graph"] = -1

        return deleted

    def _wal_checkpoint(self, db_paths: List[Path]) -> Dict[str, str]:
        """对所有数据库执行 WAL checkpoint(TRUNCATE)。"""
        results: Dict[str, str] = {}
        for path in db_paths:
            if self._is_non_sqlite_payload(path):
                results[path.name] = "skipped_non_sqlite_payload"
                continue
            if not path.exists():
                results[path.name] = "missing"
                continue
            try:
                with sqlite3.connect(str(path), timeout=10) as conn:
                    conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
                    results[path.name] = "ok"
            except MAINTENANCE_OPERATION_ERRORS as exc:
                logger.warning("[DBMaintenance] checkpoint %s failed: %s", path.name, exc)
                results[path.name] = "error"
        return results

    def _optimize(self, db_paths: List[Path]) -> Dict[str, str]:
        """对所有数据库执行 PRAGMA optimize。"""
        results: Dict[str, str] = {}
        for path in db_paths:
            if self._is_non_sqlite_payload(path):
                results[path.name] = "skipped_non_sqlite_payload"
                continue
            if not path.exists():
                results[path.name] = "missing"
                continue
            try:
                with sqlite3.connect(str(path), timeout=10) as conn:
                    conn.execute("PRAGMA optimize")
                    results[path.name] = "ok"
            except MAINTENANCE_OPERATION_ERRORS as exc:
                logger.warning("[DBMaintenance] optimize %s failed: %s", path.name, exc)
                results[path.name] = "error"
        return results

    def _should_vacuum(self) -> bool:
        """判断今天是否是配置的 VACUUM 执行日（0=周日）。"""
        target = int(self.config.get("storage.maintenance.vacuum_day_of_week", 0))
        # datetime.weekday(): 周一=0；转换为周日=0
        today = (datetime.now().weekday() + 1) % 7
        return today == target

    def _vacuum_large_dbs(self, db_paths: List[Path]) -> Dict[str, str]:
        """对超过阈值的 SQLite 数据库执行 VACUUM。"""
        threshold_mb = float(self.config.get("storage.maintenance.vacuum_size_threshold_mb", 100))
        threshold_bytes = threshold_mb * 1024 * 1024
        results: Dict[str, str] = {}
        for path in db_paths:
            if self._is_non_sqlite_payload(path):
                results[path.name] = "skipped_non_sqlite_payload"
                continue
            if not path.exists():
                results[path.name] = "missing"
                continue
            if path.stat().st_size < threshold_bytes:
                results[path.name] = "below_threshold"
                continue
            try:
                with sqlite3.connect(str(path), timeout=30) as conn:
                    conn.execute("VACUUM")
                    results[path.name] = "ok"
            except MAINTENANCE_OPERATION_ERRORS as exc:
                logger.warning("[DBMaintenance] vacuum %s failed: %s", path.name, exc)
                results[path.name] = "error"
        return results

    def run(self, *, dry_run: bool = False, force: bool = False) -> Dict[str, Any]:
        """执行一次数据库维护。

        Args:
            dry_run: 为 True 时只统计将要清理的行数，不实际删除。
            force: 为 True 时跳过 interval_hours 间隔检查。

        Returns:
            包含 deleted/wal_checkpoint/optimize/vacuum 等统计的字典。
        """
        now = time.time()
        interval_hours = float(self.config.get("storage.maintenance.interval_hours", 24))
        if not force and now - self._last_run < interval_hours * 3600:
            return {"skipped": True, "reason": "too_soon", "last_run": self._last_run}

        self._last_run = now
        db_paths = list(self._db_paths().values())

        logger.info("[DBMaintenance] starting dry_run=%s", dry_run)
        deleted = self._cleanup_stores(dry_run=dry_run)
        checkpoint = self._wal_checkpoint(db_paths)
        optimize = self._optimize(db_paths)
        vacuum: Dict[str, str] = {}
        if self._should_vacuum():
            vacuum = self._vacuum_large_dbs(db_paths)

        result: Dict[str, Any] = {
            "dry_run": dry_run,
            "deleted": deleted,
            "wal_checkpoint": checkpoint,
            "optimize": optimize,
            "vacuum": vacuum,
            "timestamp": datetime.now().isoformat(),
        }
        logger.info("[DBMaintenance] completed: %s", result)
        return result
