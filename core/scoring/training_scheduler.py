# -*- coding: utf-8 -*-
"""
training_scheduler.py — 评分模型训练调度器

职责：
  - on_buffer_full(dimension): buffer 满时立即训练
  - on_hourly_tick(): 每小时消费延迟信号
  - on_daily_cleanup(): 清理旧模型版本

由 chronos 注册定时任务调用。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Callable, Dict, List, Optional

logger = logging.getLogger(__name__)


@dataclass
class TrainingJob:
    """训练任务记录"""
    dimension: str
    triggered_by: str          # buffer_full / hourly / manual
    status: str = "pending"    # pending / running / completed / failed
    started_at: Optional[datetime] = None
    finished_at: Optional[datetime] = None
    samples_used: int = 0
    model_version: str = ""
    error_msg: str = ""


class ScorerTrainingScheduler:
    """评分模型训练调度器"""

    def __init__(
        self,
        train_fn: Callable[[str], Dict[str, Any]],
        cleanup_fn: Optional[Callable[[str, int], int]] = None,
        max_versions: int = 5,
    ):
        """
        Args:
            train_fn: 训练函数，接收 dimension，返回 {"success": bool, "version": str, "samples": int}
            cleanup_fn: 清理函数，接收 (dimension, keep_n)，返回删除数量
            max_versions: 每个维度保留的最大模型版本数
        """
        self.train_fn = train_fn
        self.cleanup_fn = cleanup_fn
        self.max_versions = max_versions
        self._jobs: List[TrainingJob] = []
        self._last_hourly = datetime.min
        self._last_daily = datetime.min

    # ── 触发接口 ──

    def on_buffer_full(self, dimension: str) -> TrainingJob:
        """buffer 满时立即触发训练"""
        job = TrainingJob(dimension=dimension, triggered_by="buffer_full")
        return self._run_training(job)

    def on_hourly_tick(self) -> List[TrainingJob]:
        """每小时触发：消费延迟信号"""
        now = datetime.now()
        if now - self._last_hourly < timedelta(minutes=50):
            return []  # 防止重复触发
        self._last_hourly = now

        # 训练所有达到阈值的维度
        results = []
        # NOTE: 实际维度列表由调用方提供，或从数据库查询
        for dim in self._get_active_dimensions():
            job = TrainingJob(dimension=dim, triggered_by="hourly")
            results.append(self._run_training(job))
        return results

    def on_daily_cleanup(self) -> Dict[str, int]:
        """每天触发：清理旧模型版本"""
        now = datetime.now()
        if now - self._last_daily < timedelta(hours=20):
            return {}
        self._last_daily = now

        cleaned = {}
        if self.cleanup_fn:
            for dim in self._get_active_dimensions():
                try:
                    count = self.cleanup_fn(dim, self.max_versions)
                    cleaned[dim] = count
                    logger.info(f"[TrainingScheduler] {dim} cleaned {count} old versions")
                except Exception as e:
                    logger.warning(f"[TrainingScheduler] cleanup failed for {dim}: {e}")
        return cleaned

    def trigger_manual(self, dimension: str) -> TrainingJob:
        """手动触发训练"""
        job = TrainingJob(dimension=dimension, triggered_by="manual")
        return self._run_training(job)

    # ── 查询接口 ──

    def get_jobs(self, dimension: Optional[str] = None, limit: int = 20) -> List[TrainingJob]:
        """获取训练历史"""
        jobs = self._jobs
        if dimension:
            jobs = [j for j in jobs if j.dimension == dimension]
        return sorted(jobs, key=lambda j: j.started_at or datetime.min, reverse=True)[:limit]

    # ── 内部方法 ──

    def _run_training(self, job: TrainingJob) -> TrainingJob:
        job.started_at = datetime.now()
        job.status = "running"
        try:
            result = self.train_fn(job.dimension)
            job.status = "completed" if result.get("success") else "failed"
            job.samples_used = result.get("samples", 0)
            job.model_version = result.get("version", "")
            if not result.get("success"):
                job.error_msg = result.get("error", "unknown")
        except Exception as e:
            job.status = "failed"
            job.error_msg = str(e)
            logger.exception(f"[TrainingScheduler] Training failed for {job.dimension}")
        finally:
            job.finished_at = datetime.now()
            self._jobs.append(job)
        return job

    def _get_active_dimensions(self) -> List[str]:
        """获取活跃维度列表（默认六域）"""
        return ["memos", "sync", "distill", "kg", "profile", "ops"]
