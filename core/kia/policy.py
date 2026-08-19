# -*- coding: utf-8 -*-
"""
EffectivePolicy — 有效策略层

为 Mnemos 自适应动态调整系统提供安全的参数生效机制：
- 全局 Config 是静态事实源（用户配置 + 默认值）。
- AdaptiveConfig 产生的调整先进入 shadow（实验状态），不直接污染全局 Config。
- 决策组件统一通过 EffectivePolicy.get() 读取当前有效参数。
- 24h 后根据 metric 决定 commit（写回全局 Config 并持久化）或 rollback（删除 shadow）。

设计目标：
1. 让自适应调整真正影响控制面决策。
2. 保留 24h 安全窗口，避免错误参数永久化。
3. 提供可观测、可审计的调整历史。
"""

from __future__ import annotations

import logging
import sqlite3
import threading
from pathlib import Path
from typing import Any, Dict, Optional

from core.config import get_config

logger = logging.getLogger(__name__)

# 模块级单例锁
_policy_lock = threading.Lock()
_policy_instance: Optional["EffectivePolicy"] = None


class EffectivePolicy:
    """
    有效策略层：合并全局 Config 与 AdaptiveConfig 的 shadow 覆盖。

    shadow 持久化到与 AdaptiveConfig 同一个 SQLite 文件，表名为 policy_shadow，
    便于统一备份/迁移。
    """

    def __init__(
        self,
        db_path: Optional[Path] = None,
        *,
        config: Any | None = None,
        initialize: bool = True,
    ):
        self._config = config or get_config()
        self.db_path = db_path or self._config.database_dir / "adaptive_config.db"
        self._lock = threading.RLock()
        self._shadows: Dict[str, Any] = {}
        if initialize:
            self._init_db()
        self._load_shadows()

    # ── 初始化与持久化 ──

    def _init_db(self):
        """Create the shadow table and ensure its current metric columns."""
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            with sqlite3.connect(str(self.db_path), timeout=10) as conn:
                conn.execute("PRAGMA journal_mode=WAL")
                conn.execute("""
                    CREATE TABLE IF NOT EXISTS policy_shadow (
                        experiment_id TEXT PRIMARY KEY,
                        config_key TEXT NOT NULL,
                        old_value REAL NOT NULL,
                        new_value REAL NOT NULL,
                        metric_before REAL,
                        metric_after REAL,
                        applied_at TEXT NOT NULL,
                        committed_at TEXT,
                        rolled_back_at TEXT
                    )
                """)
                # 兼容已存在的旧表：补加 metric_after 列
                existing_cols = {row[1] for row in conn.execute("PRAGMA table_info(policy_shadow)")}
                if "metric_after" not in existing_cols:
                    conn.execute("ALTER TABLE policy_shadow ADD COLUMN metric_after REAL")
                conn.commit()
        except (sqlite3.Error, OSError) as exc:
            logger.warning("[EffectivePolicy] shadow 表初始化失败: %s", exc)

    def _load_shadows(self):
        """从数据库加载尚未 commit/rollback 的 shadow。"""
        if not self.db_path.is_file():
            self._shadows = {}
            return
        try:
            with sqlite3.connect(
                self.db_path.resolve().as_uri() + "?mode=ro",
                uri=True,
                timeout=10,
            ) as conn:
                rows = conn.execute("""
                    SELECT config_key, new_value
                    FROM policy_shadow
                    WHERE committed_at IS NULL AND rolled_back_at IS NULL
                """).fetchall()
            with self._lock:
                self._shadows = {key: self._coerce_number(value) for key, value in rows}
        except (sqlite3.Error, OSError) as exc:
            logger.warning("[EffectivePolicy] shadow 加载失败: %s", exc)
            self._shadows = {}

    @staticmethod
    def _coerce_number(value: Any) -> Any:
        """把数据库中的 int/float 统一成 float（若原值是小数）。"""
        if isinstance(value, (int, float)):
            return float(value)
        return value

    # ── 公共 API ──

    def get(self, key: str, default: Any | None = None) -> Any:
        """
        读取有效配置值，优先级：
        1. 当前未回滚的 shadow override
        2. 全局 Config
        3. default
        """
        with self._lock:
            if key in self._shadows:
                value = self._shadows[key]
                from core.ops.runtime_flow_telemetry import (
                    record_runtime_consumed,
                    record_runtime_produced,
                    runtime_item_id,
                )

                item_id = runtime_item_id("adaptive-policy", key)
                record_runtime_produced(
                    "adaptive_config_to_runtime_weights",
                    source="core/kia/policy.py",
                    item_id=item_id,
                    intended_consumers=["core/kia/policy.py"],
                    metadata={"transition": "active_shadow_read", "config_key": key},
                    idempotency_key=runtime_item_id("adaptive-shadow-version", key, value),
                    config_or_path=self.db_path.parent,
                )
                record_runtime_consumed(
                    "adaptive_config_to_runtime_weights",
                    source="core/kia/policy.py",
                    item_id=item_id,
                    metadata={"transition": "policy_shadow_consumed", "config_key": key},
                    config_or_path=self.db_path.parent,
                )
                return value
        return self._config.get(key, default)

    def set_shadow(
        self,
        config_key: str,
        new_value: float,
        experiment_id: Optional[str] = None,
        metric_before: Optional[float] = None,
    ) -> str:
        """
        创建 shadow 覆盖。

        Args:
            config_key: 点号路径，如 "distill.trigger_threshold"
            new_value: 新值
            experiment_id: 实验标识，默认使用 config_key
            metric_before: 调整前的 metric EWMA，用于 rollback 判断

        Returns:
            experiment_id
        """
        experiment_id = experiment_id or config_key
        old_value = self.get(config_key)
        # 尽量把 old_value 转成数值；若全局 Config 无此键，用 new_value 兜底
        try:
            old_value = float(old_value) if old_value is not None else new_value
        except (TypeError, ValueError):
            old_value = new_value

        with self._lock:
            self._shadows[config_key] = self._coerce_number(new_value)

        try:
            with sqlite3.connect(str(self.db_path), timeout=10) as conn:
                conn.execute(
                    """
                    INSERT OR REPLACE INTO policy_shadow
                    (experiment_id, config_key, old_value, new_value, metric_before,
                     applied_at, committed_at, rolled_back_at)
                    VALUES (?, ?, ?, ?, ?, ?, NULL, NULL)
                """,
                    (
                        experiment_id,
                        config_key,
                        old_value,
                        new_value,
                        metric_before,
                        _now_iso(),
                    ),
                )
                conn.commit()
        except (sqlite3.Error, OSError) as exc:
            logger.warning("[EffectivePolicy] shadow 持久化失败: %s", exc)

        logger.info(
            "[EffectivePolicy] shadow 创建: %s %.4f → %.4f",
            config_key,
            old_value,
            new_value,
        )
        return experiment_id

    def commit_or_rollback(self, experiment_id: str, metric_after: Optional[float] = None) -> bool:
        """
        根据调整后的 metric 决定 commit 还是 rollback。

        判断逻辑与 AdaptiveConfig 保持一致：
        - new_value > old_value 时，期望 metric 下降；若 metric_after > metric_before * 1.1 则恶化。
        - new_value < old_value 时，期望 metric 上升；若 metric_after < metric_before * 0.9 则恶化。

        Returns:
            True 表示 commit，False 表示 rollback。
        """
        try:
            with sqlite3.connect(str(self.db_path), timeout=10) as conn:
                row = conn.execute(
                    """
                    SELECT config_key, old_value, new_value, metric_before
                    FROM policy_shadow
                    WHERE experiment_id = ? AND committed_at IS NULL
                      AND rolled_back_at IS NULL
                """,
                    (experiment_id,),
                ).fetchone()
        except (sqlite3.Error, OSError) as exc:
            logger.warning("[EffectivePolicy] 查询 shadow 失败: %s", exc)
            return False

        if row is None:
            logger.debug("[EffectivePolicy] 无有效 shadow: %s", experiment_id)
            return False

        config_key, old_value, new_value, metric_before = row
        worsened = self._is_worsened(old_value, new_value, metric_before, metric_after)

        if worsened:
            self._rollback(experiment_id, config_key, old_value, metric_after)
            return False
        else:
            self._commit(experiment_id, config_key, new_value, metric_after)
            return True

    def force_commit(self, experiment_id: str) -> bool:
        """强制 commit（用于测试或人工确认）。"""
        try:
            with sqlite3.connect(str(self.db_path), timeout=10) as conn:
                row = conn.execute(
                    """
                    SELECT config_key, new_value
                    FROM policy_shadow
                    WHERE experiment_id = ? AND committed_at IS NULL
                      AND rolled_back_at IS NULL
                """,
                    (experiment_id,),
                ).fetchone()
        except (sqlite3.Error, OSError) as exc:
            logger.warning("[EffectivePolicy] 查询 shadow 失败: %s", exc)
            return False

        if row is None:
            return False
        config_key, new_value = row
        self._commit(experiment_id, config_key, new_value, None)
        return True

    def force_rollback(self, experiment_id: str) -> bool:
        """强制 rollback（用于测试或人工确认）。"""
        try:
            with sqlite3.connect(str(self.db_path), timeout=10) as conn:
                row = conn.execute(
                    """
                    SELECT config_key, old_value
                    FROM policy_shadow
                    WHERE experiment_id = ? AND committed_at IS NULL
                      AND rolled_back_at IS NULL
                """,
                    (experiment_id,),
                ).fetchone()
        except (sqlite3.Error, OSError) as exc:
            logger.warning("[EffectivePolicy] 查询 shadow 失败: %s", exc)
            return False

        if row is None:
            return False
        config_key, old_value = row
        self._rollback(experiment_id, config_key, old_value, None)
        return True

    def list_shadows(self) -> Dict[str, Dict[str, Any]]:
        """列出所有未 commit/rollback 的 shadow。"""
        if not self.db_path.is_file():
            return {}
        try:
            with sqlite3.connect(
                self.db_path.resolve().as_uri() + "?mode=ro",
                uri=True,
                timeout=10,
            ) as conn:
                rows = conn.execute("""
                    SELECT experiment_id, config_key, old_value, new_value,
                           metric_before, applied_at
                    FROM policy_shadow
                    WHERE committed_at IS NULL AND rolled_back_at IS NULL
                """).fetchall()
            return {
                config_key: {
                    "experiment_id": experiment_id,
                    "old_value": old_value,
                    "new_value": new_value,
                    "metric_before": metric_before,
                    "applied_at": applied_at,
                }
                for experiment_id, config_key, old_value, new_value, metric_before, applied_at in rows  # noqa: E501
            }
        except (sqlite3.Error, OSError) as exc:
            logger.warning("[EffectivePolicy] shadow 列表读取失败: %s", exc)
            return {}

    # ── 内部方法 ──

    def _is_worsened(
        self,
        old_value: float,
        new_value: float,
        metric_before: Optional[float],
        metric_after: Optional[float],
    ) -> bool:
        """判断调整是否恶化。"""
        if not metric_before or not metric_after:
            # 缺少 metric，保守不判断为恶化，允许 commit
            return False
        if new_value > old_value:
            return metric_after > metric_before * 1.1
        else:
            return metric_after < metric_before * 0.9

    def _commit(
        self, experiment_id: str, config_key: str, new_value: float, metric_after: Optional[float]
    ):
        """提交 shadow：写回全局 Config 并持久化，然后删除 shadow。"""
        try:
            self._config.set(config_key, new_value)
            self._config.save()
        except (OSError, RuntimeError, ValueError, TypeError, KeyError) as exc:
            logger.error("[EffectivePolicy] commit 写全局 Config 失败: %s", exc)
            return

        with self._lock:
            self._shadows.pop(config_key, None)

        try:
            with sqlite3.connect(str(self.db_path), timeout=10) as conn:
                conn.execute(
                    """
                    UPDATE policy_shadow
                    SET committed_at = ?, metric_after = ?
                    WHERE experiment_id = ?
                """,
                    (_now_iso(), metric_after, experiment_id),
                )
                conn.commit()
        except (sqlite3.Error, OSError) as exc:
            logger.warning("[EffectivePolicy] commit 记录失败: %s", exc)

        logger.info(
            "[EffectivePolicy] commit: %s = %.4f 已写回全局 Config",
            config_key,
            new_value,
        )

    def _rollback(
        self, experiment_id: str, config_key: str, old_value: float, metric_after: Optional[float]
    ):
        """回滚 shadow：删除 shadow，全局 Config 保持原值。"""
        with self._lock:
            self._shadows.pop(config_key, None)

        try:
            with sqlite3.connect(str(self.db_path), timeout=10) as conn:
                conn.execute(
                    """
                    UPDATE policy_shadow
                    SET rolled_back_at = ?, metric_after = ?
                    WHERE experiment_id = ?
                """,
                    (_now_iso(), metric_after, experiment_id),
                )
                conn.commit()
        except (sqlite3.Error, OSError) as exc:
            logger.warning("[EffectivePolicy] rollback 记录失败: %s", exc)

        logger.info(
            "[EffectivePolicy] rollback: %s 恢复 %.4f",
            config_key,
            old_value,
        )


def get_effective_policy() -> EffectivePolicy:
    """获取全局 EffectivePolicy 单例。"""
    global _policy_instance
    if _policy_instance is None:
        with _policy_lock:
            if _policy_instance is None:
                _policy_instance = EffectivePolicy()
    return _policy_instance


def get_shadowed_value(key: str, default: Any | None = None) -> Any:
    """
    Return an adaptive shadow value only when an active shadow exists.

    Runtime consumers often receive an explicit config object in tests or
    embedded flows.  This helper preserves that caller-provided default unless
    AdaptiveConfig has an active shadow override for the key.
    """
    try:
        policy = get_effective_policy()
        if key in policy.list_shadows():
            return policy.get(key, default)
    except (
        AttributeError,
        OSError,
        RuntimeError,
        TypeError,
        ValueError,
        sqlite3.Error,
    ) as exc:  # pragma: no cover - defensive policy fallback
        logger.debug("[EffectivePolicy] shadow 覆盖读取失败: %s", exc, exc_info=True)
    return default


def _now_iso() -> str:
    from datetime import datetime

    return datetime.now().isoformat()
