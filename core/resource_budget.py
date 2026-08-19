"""
ResourceBudget — 资源预算治理层

职责：
1. 监测 CPU、内存、队列深度、电源/温度状态
2. 为后台服务提供 budget 申请接口
3. 动态降速而非关闭功能

优先级：
- P0: capture worker（不可中断）
- P1: L1 storage 写入、L1 同步
- P2: 蒸馏、embedding 构建
- P3: 画像扫描、KIA 调度
"""

from __future__ import annotations

import logging
import subprocess
import threading
import time
from dataclasses import dataclass
from typing import Dict, Optional

# Constants extracted from magic numbers
RESOURCE_BUDGET_SNAPSHOT_TTL = 30
CUTOFF_SECONDS = 3600

logger = logging.getLogger(__name__)


@dataclass
class ResourceSnapshot:
    """资源快照"""

    cpu_percent: float = 0.0
    memory_percent: float = 0.0
    thermal_state: str = "unknown"  # normal, fair, serious, critical
    power_source: str = "unknown"  # ac, battery
    queue_depth: int = 0


class ResourceBudget:
    """资源预算管理器"""

    # 阈值配置
    CPU_THROTTLE = 70.0  # CPU 超过此值开始节流
    CPU_PAUSE = 90.0  # CPU 超过此值暂停低优先级任务
    MEM_THROTTLE = 80.0  # 内存超过此值开始节流
    MEM_PAUSE = 95.0  # 内存超过此值暂停低优先级任务

    # 服务优先级
    PRIORITY = {
        "capture_worker": 0,
        "raw_sync": 1,
        "l1_sync": 1,
        "distill": 2,
        "embedding_build": 2,
        "claude_live_sync": 2,
        "event_bus": 2,
        "heartbeat": 3,
        "inbox_scan": 3,
        "signal_collect": 3,
        "persona_scan": 3,
        "persona_analysis": 3,
        "kia_sched": 3,
        "verification_queue": 3,
    }

    # 快照过期时间（秒）
    SNAPSHOT_TTL = RESOURCE_BUDGET_SNAPSHOT_TTL

    # 历史记录上限（约 1 小时 @ 30s 采样间隔）
    MAX_HISTORY = 120

    def __init__(self):
        self._last_snapshot: Optional[ResourceSnapshot] = None
        self._last_snapshot_time: float = 0.0
        self._history: list[tuple[float, float, float]] = []  # (timestamp, cpu%, mem%)
        self._history_lock = threading.Lock()
        self._psutil_available = False
        try:
            import psutil  # noqa: F401

            self._psutil_available = True
        except ImportError:
            logger.debug("[resource_budget] ImportError suppressed", exc_info=True)

    def snapshot(self) -> ResourceSnapshot:
        """采集当前资源快照"""
        snap = ResourceSnapshot()

        if self._psutil_available:
            import psutil

            try:
                snap.cpu_percent = psutil.cpu_percent(interval=0.5)
                snap.memory_percent = psutil.virtual_memory().percent
            except (
                OSError, ValueError, TypeError, KeyError, ImportError, AttributeError, RuntimeError,
                subprocess.SubprocessError
            ):
                logging.getLogger(__name__).warning("Unexpected error", exc_info=True)

        # macOS thermal state
        snap.thermal_state = self._macos_thermal_state()

        # 电源状态
        snap.power_source = self._power_source()

        self._last_snapshot = snap
        self._last_snapshot_time = time.time()

        # [E] 记录轻量历史，用于性能基准趋势（线程安全）
        with self._history_lock:
            self._history.append((self._last_snapshot_time, snap.cpu_percent, snap.memory_percent))
            if len(self._history) > self.MAX_HISTORY:
                self._history.pop(0)

        return snap

    def _macos_thermal_state(self) -> str:
        """获取 macOS 温度状态"""
        try:
            result = subprocess.run(
                ["pmset", "-g", "therm"], capture_output=True, text=True, timeout=2
            )
            output = result.stdout.lower()
            if "critical" in output:
                return "critical"
            elif "danger" in output or "serious" in output:
                return "serious"
            elif "fair" in output:
                return "fair"
            elif "normal" in output:
                return "normal"
            return "unknown"
        except (OSError, subprocess.SubprocessError):
            return "unknown"

    def _power_source(self) -> str:
        """获取电源状态"""
        try:
            result = subprocess.run(
                ["pmset", "-g", "ac"], capture_output=True, text=True, timeout=2
            )
            if "ac power" in result.stdout.lower():
                return "ac"
            elif "battery" in result.stdout.lower():
                return "battery"
            return "unknown"
        except (OSError, subprocess.SubprocessError):
            return "unknown"

    def _ensure_snapshot(self) -> ResourceSnapshot:
        """获取有效快照：缓存未过期则复用，否则重新采样"""
        if (
            self._last_snapshot is None
            or (time.time() - self._last_snapshot_time) > self.SNAPSHOT_TTL
        ):
            self.snapshot()
        # snapshot() 保证 _last_snapshot 非 None
        return self._last_snapshot  # type: ignore[return-value]

    def can_run(self, service: str) -> bool:
        """检查指定服务是否可以运行"""
        snap = self._ensure_snapshot()
        priority = self.PRIORITY.get(service, 2)

        # P0 服务（capture）始终允许运行
        if priority <= 0:
            return True

        # 温度临界时只保留 P0/P1
        if snap.thermal_state == "critical":
            return priority <= 1
        if snap.thermal_state == "serious":
            return priority <= 1

        # CPU 过高时暂停 P2/P3
        if snap.cpu_percent >= self.CPU_PAUSE:
            return priority <= 1
        if snap.cpu_percent >= self.CPU_THROTTLE:
            return priority <= 2

        # 内存过高时暂停 P2/P3
        if snap.memory_percent >= self.MEM_PAUSE:
            return priority <= 1
        if snap.memory_percent >= self.MEM_THROTTLE:
            return priority <= 2

        # 电池供电时降低 P3 优先级
        if snap.power_source == "battery" and priority >= 3:
            return False

        return True

    def throttle_delay(self, service: str) -> float:
        """返回该服务应等待的秒数（动态降速）"""
        snap = self._ensure_snapshot()

        priority = self.PRIORITY.get(service, 2)

        # P0 不延迟
        if priority <= 0:
            return 0.0

        delay = 0.0

        # CPU 负载延迟
        if snap.cpu_percent >= self.CPU_PAUSE:
            delay = max(delay, 30.0)
        elif snap.cpu_percent >= self.CPU_THROTTLE:
            delay = max(delay, 10.0)

        # 内存负载延迟
        if snap.memory_percent >= self.MEM_PAUSE:
            delay = max(delay, 30.0)
        elif snap.memory_percent >= self.MEM_THROTTLE:
            delay = max(delay, 10.0)

        # 温度延迟
        if snap.thermal_state == "critical":
            delay = max(delay, 60.0)
        elif snap.thermal_state == "serious":
            delay = max(delay, 30.0)
        elif snap.thermal_state == "fair":
            delay = max(delay, 5.0)

        # 电池供电延迟
        if snap.power_source == "battery":
            delay = max(delay, 15.0)

        return delay

    def history_stats(self, hours: float = 1.0) -> Dict[str, float]:
        """返回最近 N 小时的资源使用统计（轻量性能基准）

        Returns:
            samples:  采样点数
            cpu_avg:  CPU 平均值 (%)
            cpu_peak: CPU 峰值 (%)
            mem_avg:  内存平均值 (%)
            mem_peak: 内存峰值 (%)
        """
        if not self._history:
            return {}
        cutoff = time.time() - hours * CUTOFF_SECONDS
        recent = [h for h in self._history if h[0] > cutoff]
        if not recent:
            return {}
        cpus = [h[1] for h in recent]
        mems = [h[2] for h in recent]
        return {
            "samples": len(recent),
            "cpu_avg": sum(cpus) / len(cpus),
            "cpu_peak": max(cpus),
            "mem_avg": sum(mems) / len(mems),
            "mem_peak": max(mems),
        }

    def status(self) -> Dict[str, str]:
        """返回当前资源状态摘要"""
        snap = self._last_snapshot or self.snapshot()
        state = "normal"

        if snap.thermal_state in ("critical", "serious"):
            state = "throttled"
        elif snap.cpu_percent >= self.CPU_PAUSE or snap.memory_percent >= self.MEM_PAUSE:
            state = "throttled"
        elif snap.cpu_percent >= self.CPU_THROTTLE or snap.memory_percent >= self.MEM_THROTTLE:
            state = "slowed"
        elif snap.power_source == "battery":
            state = "battery"

        return {
            "state": state,
            "cpu": f"{snap.cpu_percent:.1f}%",
            "memory": f"{snap.memory_percent:.1f}%",
            "thermal": snap.thermal_state,
            "power": snap.power_source,
        }


# 全局实例
_budget_instance: Optional[ResourceBudget] = None


def get_budget() -> ResourceBudget:
    """获取全局 ResourceBudget 实例"""
    global _budget_instance
    if _budget_instance is None:
        _budget_instance = ResourceBudget()
    return _budget_instance
