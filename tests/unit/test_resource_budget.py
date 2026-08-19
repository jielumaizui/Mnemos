# -*- coding: utf-8 -*-
"""Unit tests for core/resource_budget.py"""

import time
from unittest.mock import MagicMock, patch


from core.resource_budget import (
    ResourceBudget,
    ResourceSnapshot,
    get_budget,
)

# ---------------------------------------------------------------------------
# ResourceSnapshot
# ---------------------------------------------------------------------------


def test_snapshot_defaults():
    """ResourceSnapshot 默认值应正确。"""
    snap = ResourceSnapshot()
    assert snap.cpu_percent == 0.0
    assert snap.memory_percent == 0.0
    assert snap.thermal_state == "unknown"
    assert snap.power_source == "unknown"
    assert snap.queue_depth == 0


def test_snapshot_custom_values():
    """ResourceSnapshot 应接受自定义值。"""
    snap = ResourceSnapshot(
        cpu_percent=50.0,
        memory_percent=60.0,
        thermal_state="fair",
        power_source="battery",
        queue_depth=10,
    )
    assert snap.cpu_percent == 50.0
    assert snap.memory_percent == 60.0
    assert snap.thermal_state == "fair"
    assert snap.power_source == "battery"
    assert snap.queue_depth == 10


# ---------------------------------------------------------------------------
# ResourceBudget init & psutil detection
# ---------------------------------------------------------------------------


class TestResourceBudgetInit:
    """ResourceBudget 初始化测试"""

    def test_init_without_psutil(self):
        """无 psutil 时应标记为不可用。"""
        with patch.dict("sys.modules", {"psutil": None}):
            rb = ResourceBudget()
            assert rb._psutil_available is False

    def test_init_with_psutil(self):
        """有 psutil 时应标记为可用。"""
        ResourceBudget()
        # 测试环境中通常有 psutil
        # 但这里我们 mock import 来确保测试稳定
        with patch(
            "builtins.__import__",
            side_effect=lambda name, *a, **k: (
                MagicMock() if name == "psutil" else __import__(name, *a, **k)
            ),
        ):
            rb2 = ResourceBudget()
            assert rb2._psutil_available is True

    def test_default_thresholds(self):
        """默认阈值应正确。"""
        rb = ResourceBudget()
        assert rb.CPU_THROTTLE == 70.0
        assert rb.CPU_PAUSE == 90.0
        assert rb.MEM_THROTTLE == 80.0
        assert rb.MEM_PAUSE == 95.0

    def test_priority_map(self):
        """优先级映射应包含关键服务。"""
        rb = ResourceBudget()
        assert rb.PRIORITY["capture_worker"] == 0
        assert rb.PRIORITY["raw_sync"] == 1
        assert rb.PRIORITY["l1_sync"] == 1
        assert rb.PRIORITY["distill"] == 2
        assert rb.PRIORITY["persona_scan"] == 3
        assert rb.PRIORITY["verification_queue"] == 3

    def test_snapshot_ttl(self):
        """快照 TTL 应为 30 秒。"""
        assert ResourceBudget.SNAPSHOT_TTL == 30

    def test_max_history(self):
        """历史记录上限应为 120。"""
        assert ResourceBudget.MAX_HISTORY == 120


# ---------------------------------------------------------------------------
# snapshot() with mocked psutil
# ---------------------------------------------------------------------------


class TestSnapshot:
    """snapshot() 测试"""

    def test_snapshot_with_psutil(self):
        """psutil 可用时应采集 CPU 和内存。"""
        rb = ResourceBudget()
        rb._psutil_available = True

        mock_psutil = MagicMock()
        mock_psutil.cpu_percent.return_value = 45.0
        mock_mem = MagicMock()
        mock_mem.percent = 55.0
        mock_psutil.virtual_memory.return_value = mock_mem

        with patch.dict("sys.modules", {"psutil": mock_psutil}):
            snap = rb.snapshot()
        assert snap.cpu_percent == 45.0
        assert snap.memory_percent == 55.0

    def test_snapshot_records_history(self):
        """snapshot 应记录历史。"""
        rb = ResourceBudget()
        rb._psutil_available = True

        mock_psutil = MagicMock()
        mock_psutil.cpu_percent.return_value = 30.0
        mock_mem = MagicMock()
        mock_mem.percent = 40.0
        mock_psutil.virtual_memory.return_value = mock_mem

        with patch.dict("sys.modules", {"psutil": mock_psutil}):
            rb.snapshot()
        assert len(rb._history) == 1
        assert rb._history[0][1] == 30.0  # cpu
        assert rb._history[0][2] == 40.0  # memory

    def test_snapshot_history_limit(self):
        """历史记录应限制在 MAX_HISTORY。"""
        rb = ResourceBudget()
        rb._psutil_available = False  # 避免 psutil 调用
        rb.MAX_HISTORY = 3  # 临时降低上限以便测试

        for i in range(5):
            rb.snapshot()
            time.sleep(0.01)  # 确保时间戳不同

        assert len(rb._history) == 3

    def test_snapshot_psutil_exception_handled(self):
        """psutil 异常应被捕获。"""
        rb = ResourceBudget()
        rb._psutil_available = True

        mock_psutil = MagicMock()
        mock_psutil.cpu_percent.side_effect = RuntimeError("psutil error")

        with patch.dict("sys.modules", {"psutil": mock_psutil}):
            # 不应抛异常
            snap = rb.snapshot()
        assert snap.cpu_percent == 0.0  # 默认值

    def test_snapshot_without_psutil(self):
        """无 psutil 时应返回默认值。"""
        rb = ResourceBudget()
        rb._psutil_available = False
        snap = rb.snapshot()
        assert snap.cpu_percent == 0.0
        assert snap.memory_percent == 0.0


# ---------------------------------------------------------------------------
# _macos_thermal_state
# ---------------------------------------------------------------------------


class TestMacosThermalState:
    """_macos_thermal_state 测试"""

    def test_critical(self):
        """应检测 critical 状态。"""
        rb = ResourceBudget()
        with patch("subprocess.run", return_value=MagicMock(stdout="TC0D critical")):
            assert rb._macos_thermal_state() == "critical"

    def test_serious(self):
        """应检测 serious/danger 状态。"""
        rb = ResourceBudget()
        with patch("subprocess.run", return_value=MagicMock(stdout="TC0D danger")):
            assert rb._macos_thermal_state() == "serious"

    def test_fair(self):
        """应检测 fair 状态。"""
        rb = ResourceBudget()
        with patch("subprocess.run", return_value=MagicMock(stdout="TC0D fair")):
            assert rb._macos_thermal_state() == "fair"

    def test_normal(self):
        """应检测 normal 状态。"""
        rb = ResourceBudget()
        with patch("subprocess.run", return_value=MagicMock(stdout="TC0D normal")):
            assert rb._macos_thermal_state() == "normal"

    def test_unknown_on_exception(self):
        """异常时应返回 unknown。"""
        rb = ResourceBudget()
        with patch("subprocess.run", side_effect=FileNotFoundError):
            assert rb._macos_thermal_state() == "unknown"

    def test_unknown_on_empty_output(self):
        """无匹配输出时应返回 unknown。"""
        rb = ResourceBudget()
        with patch("subprocess.run", return_value=MagicMock(stdout="no match")):
            assert rb._macos_thermal_state() == "unknown"


# ---------------------------------------------------------------------------
# _power_source
# ---------------------------------------------------------------------------


class TestPowerSource:
    """_power_source 测试"""

    def test_ac_power(self):
        """应检测 AC 电源。"""
        rb = ResourceBudget()
        with patch("subprocess.run", return_value=MagicMock(stdout="AC Power")):
            assert rb._power_source() == "ac"

    def test_battery(self):
        """应检测电池供电。"""
        rb = ResourceBudget()
        with patch("subprocess.run", return_value=MagicMock(stdout="Battery")):
            assert rb._power_source() == "battery"

    def test_unknown_on_exception(self):
        """异常时应返回 unknown。"""
        rb = ResourceBudget()
        with patch("subprocess.run", side_effect=FileNotFoundError):
            assert rb._power_source() == "unknown"

    def test_unknown_on_empty_output(self):
        """无匹配输出时应返回 unknown。"""
        rb = ResourceBudget()
        with patch("subprocess.run", return_value=MagicMock(stdout="no match")):
            assert rb._power_source() == "unknown"


# ---------------------------------------------------------------------------
# _ensure_snapshot (cache logic)
# ---------------------------------------------------------------------------


class TestEnsureSnapshot:
    """_ensure_snapshot 缓存逻辑测试"""

    def test_creates_snapshot_when_none(self, monkeypatch):
        """无快照时应创建。"""
        rb = ResourceBudget()
        rb._psutil_available = False
        snap = rb._ensure_snapshot()
        assert snap is not None
        assert rb._last_snapshot is not None

    def test_reuses_fresh_snapshot(self):
        """未过期快照应复用。"""
        rb = ResourceBudget()
        rb._last_snapshot = ResourceSnapshot(cpu_percent=42.0)
        rb._last_snapshot_time = time.time()
        snap = rb._ensure_snapshot()
        assert snap.cpu_percent == 42.0

    def test_refreshes_expired_snapshot(self, monkeypatch):
        """过期快照应刷新。"""
        rb = ResourceBudget()
        rb._psutil_available = False
        rb._last_snapshot = ResourceSnapshot(cpu_percent=10.0)
        rb._last_snapshot_time = time.time() - 60  # 超过 TTL
        snap = rb._ensure_snapshot()
        # snapshot() 会重新采集，默认值为 0
        assert snap.cpu_percent == 0.0


# ---------------------------------------------------------------------------
# can_run
# ---------------------------------------------------------------------------


class TestCanRun:
    """can_run 测试"""

    def test_p0_always_runs(self):
        """P0 服务应始终允许运行。"""
        rb = ResourceBudget()
        rb._last_snapshot = ResourceSnapshot(
            cpu_percent=99.0,
            memory_percent=99.0,
            thermal_state="critical",
        )
        rb._last_snapshot_time = time.time()
        assert rb.can_run("capture_worker") is True

    def test_critical_thermal_allows_p0_p1(self):
        """critical 温度应只允许 P0/P1。"""
        rb = ResourceBudget()
        rb._last_snapshot = ResourceSnapshot(thermal_state="critical")
        rb._last_snapshot_time = time.time()
        assert rb.can_run("raw_sync") is True
        assert rb.can_run("l1_sync") is True
        assert rb.can_run("distill") is False
        assert rb.can_run("persona_scan") is False

    def test_serious_thermal_allows_p0_p1(self):
        """serious 温度应只允许 P0/P1。"""
        rb = ResourceBudget()
        rb._last_snapshot = ResourceSnapshot(thermal_state="serious")
        rb._last_snapshot_time = time.time()
        assert rb.can_run("raw_sync") is True
        assert rb.can_run("distill") is False

    def test_high_cpu_pauses_p2_p3(self):
        """CPU >= 90% 应暂停 P2/P3。"""
        rb = ResourceBudget()
        rb._last_snapshot = ResourceSnapshot(cpu_percent=95.0)
        rb._last_snapshot_time = time.time()
        assert rb.can_run("raw_sync") is True
        assert rb.can_run("distill") is False
        assert rb.can_run("persona_scan") is False

    def test_moderate_cpu_throttles_p3(self):
        """CPU 70-90% 应节流 P3。"""
        rb = ResourceBudget()
        rb._last_snapshot = ResourceSnapshot(cpu_percent=75.0)
        rb._last_snapshot_time = time.time()
        assert rb.can_run("raw_sync") is True
        assert rb.can_run("distill") is True
        assert rb.can_run("persona_scan") is False

    def test_high_memory_pauses_p2_p3(self):
        """内存 >= 95% 应暂停 P2/P3。"""
        rb = ResourceBudget()
        rb._last_snapshot = ResourceSnapshot(memory_percent=98.0)
        rb._last_snapshot_time = time.time()
        assert rb.can_run("raw_sync") is True
        assert rb.can_run("distill") is False

    def test_battery_blocks_p3(self):
        """电池供电应阻止 P3。"""
        rb = ResourceBudget()
        rb._last_snapshot = ResourceSnapshot(
            power_source="battery",
            cpu_percent=10.0,
            memory_percent=10.0,
        )
        rb._last_snapshot_time = time.time()
        assert rb.can_run("distill") is True
        assert rb.can_run("persona_scan") is False

    def test_normal_conditions_all_allowed(self):
        """正常条件下所有服务应允许。"""
        rb = ResourceBudget()
        rb._last_snapshot = ResourceSnapshot(
            cpu_percent=10.0,
            memory_percent=10.0,
            power_source="ac",
        )
        rb._last_snapshot_time = time.time()
        assert rb.can_run("capture_worker") is True
        assert rb.can_run("raw_sync") is True
        assert rb.can_run("distill") is True
        assert rb.can_run("persona_scan") is True

    def test_unknown_service_defaults_to_priority_2(self):
        """未知服务默认优先级应为 2，throttle 下允许运行。"""
        rb = ResourceBudget()
        rb._last_snapshot = ResourceSnapshot(cpu_percent=75.0)
        rb._last_snapshot_time = time.time()
        assert rb.can_run("unknown_service") is True  # priority=2, throttle 允许 P2

    def test_unknown_service_paused_when_cpu_high(self):
        """未知服务（priority=2）在 CPU pause 时应被阻止。"""
        rb = ResourceBudget()
        rb._last_snapshot = ResourceSnapshot(cpu_percent=95.0)
        rb._last_snapshot_time = time.time()
        assert rb.can_run("unknown_service") is False  # priority=2, pause 只允许 P0/P1


# ---------------------------------------------------------------------------
# throttle_delay
# ---------------------------------------------------------------------------


class TestThrottleDelay:
    """throttle_delay 测试"""

    def test_p0_no_delay(self):
        """P0 服务应无延迟。"""
        rb = ResourceBudget()
        rb._last_snapshot = ResourceSnapshot(
            cpu_percent=99.0,
            thermal_state="critical",
            power_source="battery",
        )
        rb._last_snapshot_time = time.time()
        assert rb.throttle_delay("capture_worker") == 0.0

    def test_critical_cpu_delay(self):
        """CPU >= 90% 应返回 30s 延迟。"""
        rb = ResourceBudget()
        rb._last_snapshot = ResourceSnapshot(cpu_percent=95.0)
        rb._last_snapshot_time = time.time()
        assert rb.throttle_delay("distill") == 30.0

    def test_throttle_cpu_delay(self):
        """CPU 70-90% 应返回 10s 延迟。"""
        rb = ResourceBudget()
        rb._last_snapshot = ResourceSnapshot(cpu_percent=75.0)
        rb._last_snapshot_time = time.time()
        assert rb.throttle_delay("distill") == 10.0

    def test_critical_memory_delay(self):
        """内存 >= 95% 应返回 30s 延迟。"""
        rb = ResourceBudget()
        rb._last_snapshot = ResourceSnapshot(memory_percent=98.0)
        rb._last_snapshot_time = time.time()
        assert rb.throttle_delay("distill") == 30.0

    def test_critical_thermal_delay(self):
        """critical 温度应返回 60s 延迟。"""
        rb = ResourceBudget()
        rb._last_snapshot = ResourceSnapshot(thermal_state="critical")
        rb._last_snapshot_time = time.time()
        assert rb.throttle_delay("distill") == 60.0

    def test_serious_thermal_delay(self):
        """serious 温度应返回 30s 延迟。"""
        rb = ResourceBudget()
        rb._last_snapshot = ResourceSnapshot(thermal_state="serious")
        rb._last_snapshot_time = time.time()
        assert rb.throttle_delay("distill") == 30.0

    def test_fair_thermal_delay(self):
        """fair 温度应返回 5s 延迟。"""
        rb = ResourceBudget()
        rb._last_snapshot = ResourceSnapshot(thermal_state="fair")
        rb._last_snapshot_time = time.time()
        assert rb.throttle_delay("distill") == 5.0

    def test_battery_delay(self):
        """电池供电应返回 15s 延迟。"""
        rb = ResourceBudget()
        rb._last_snapshot = ResourceSnapshot(power_source="battery")
        rb._last_snapshot_time = time.time()
        assert rb.throttle_delay("distill") == 15.0

    def test_combined_delay_takes_max(self):
        """多个条件同时满足时应取最大延迟。"""
        rb = ResourceBudget()
        rb._last_snapshot = ResourceSnapshot(
            cpu_percent=95.0,  # 30s
            thermal_state="critical",  # 60s
            power_source="battery",  # 15s
        )
        rb._last_snapshot_time = time.time()
        assert rb.throttle_delay("distill") == 60.0

    def test_normal_no_delay(self):
        """正常条件应无延迟。"""
        rb = ResourceBudget()
        rb._last_snapshot = ResourceSnapshot(
            cpu_percent=10.0,
            memory_percent=10.0,
            power_source="ac",
        )
        rb._last_snapshot_time = time.time()
        assert rb.throttle_delay("distill") == 0.0


# ---------------------------------------------------------------------------
# history_stats
# ---------------------------------------------------------------------------


class TestHistoryStats:
    """history_stats 测试"""

    def test_empty_history(self):
        """空历史应返回空字典。"""
        rb = ResourceBudget()
        assert rb.history_stats() == {}

    def test_single_sample(self):
        """单条历史应正确统计。"""
        rb = ResourceBudget()
        now = time.time()
        rb._history = [(now, 50.0, 60.0)]
        stats = rb.history_stats()
        assert stats["samples"] == 1
        assert stats["cpu_avg"] == 50.0
        assert stats["cpu_peak"] == 50.0
        assert stats["mem_avg"] == 60.0
        assert stats["mem_peak"] == 60.0

    def test_multiple_samples(self):
        """多条历史应正确统计平均值和峰值。"""
        rb = ResourceBudget()
        now = time.time()
        rb._history = [
            (now - 60, 30.0, 40.0),
            (now - 30, 50.0, 60.0),
            (now, 70.0, 80.0),
        ]
        stats = rb.history_stats()
        assert stats["samples"] == 3
        assert stats["cpu_avg"] == 50.0
        assert stats["cpu_peak"] == 70.0
        assert stats["mem_avg"] == 60.0
        assert stats["mem_peak"] == 80.0

    def test_filters_by_hours(self):
        """应按要求过滤时间范围。"""
        rb = ResourceBudget()
        now = time.time()
        rb._history = [
            (now - 7200, 30.0, 40.0),  # 2小时前
            (now - 1800, 50.0, 60.0),  # 30分钟前
            (now, 70.0, 80.0),
        ]
        stats = rb.history_stats(hours=1.0)
        # 只应包含最近1小时的数据
        assert stats["samples"] == 2
        assert stats["cpu_avg"] == 60.0

    def test_no_recent_samples(self):
        """无近期样本应返回空字典。"""
        rb = ResourceBudget()
        now = time.time()
        rb._history = [(now - 7200, 30.0, 40.0)]
        assert rb.history_stats(hours=1.0) == {}


# ---------------------------------------------------------------------------
# status
# ---------------------------------------------------------------------------


class TestStatus:
    """status 测试"""

    def test_normal_status(self):
        """正常状态应返回 normal。"""
        rb = ResourceBudget()
        rb._last_snapshot = ResourceSnapshot(
            cpu_percent=10.0,
            memory_percent=10.0,
            power_source="ac",
            thermal_state="normal",
        )
        st = rb.status()
        assert st["state"] == "normal"
        assert st["cpu"] == "10.0%"
        assert st["memory"] == "10.0%"
        assert st["thermal"] == "normal"
        assert st["power"] == "ac"

    def test_throttled_status(self):
        """critical 温度应返回 throttled。"""
        rb = ResourceBudget()
        rb._last_snapshot = ResourceSnapshot(
            cpu_percent=10.0,
            thermal_state="critical",
        )
        st = rb.status()
        assert st["state"] == "throttled"

    def test_cpu_pause_throttled(self):
        """高 CPU 应返回 throttled。"""
        rb = ResourceBudget()
        rb._last_snapshot = ResourceSnapshot(cpu_percent=95.0)
        st = rb.status()
        assert st["state"] == "throttled"

    def test_cpu_throttle_slowed(self):
        """中等 CPU 应返回 slowed。"""
        rb = ResourceBudget()
        rb._last_snapshot = ResourceSnapshot(cpu_percent=75.0)
        st = rb.status()
        assert st["state"] == "slowed"

    def test_memory_throttle_slowed(self):
        """中等内存应返回 slowed。"""
        rb = ResourceBudget()
        rb._last_snapshot = ResourceSnapshot(memory_percent=85.0)
        st = rb.status()
        assert st["state"] == "slowed"

    def test_battery_status(self):
        """电池供电应返回 battery。"""
        rb = ResourceBudget()
        rb._last_snapshot = ResourceSnapshot(
            cpu_percent=10.0,
            power_source="battery",
        )
        st = rb.status()
        assert st["state"] == "battery"

    def test_creates_snapshot_when_none(self):
        """无快照时应自动创建。"""
        rb = ResourceBudget()
        rb._last_snapshot = None
        rb._psutil_available = False
        st = rb.status()
        assert st["state"] == "normal"
        assert rb._last_snapshot is not None


# ---------------------------------------------------------------------------
# get_budget singleton
# ---------------------------------------------------------------------------


class TestGetBudget:
    """get_budget 全局实例测试"""

    def test_returns_same_instance(self):
        """多次调用应返回同一实例。"""
        b1 = get_budget()
        b2 = get_budget()
        assert b1 is b2

    def test_is_resource_budget(self):
        """应返回 ResourceBudget 实例。"""
        b = get_budget()
        assert isinstance(b, ResourceBudget)
