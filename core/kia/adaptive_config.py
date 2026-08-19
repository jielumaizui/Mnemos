"""
AdaptiveConfig — 自适应配置

【E14 全库修复】E8 知识轨迹完整实现。
根据使用统计自动调整系统参数。
"""

import logging
import sqlite3
from collections import deque
from typing import Dict, Optional, Any
from datetime import datetime, timedelta
from pathlib import Path

from core.config import get_config
from core.kia.adaptive_policy_matrix import (
    DEFAULT_ADAPTIVE_POLICY_RULES,
    build_adaptive_policy_report,
)
from core.kia.policy import EffectivePolicy, get_effective_policy

# Constants extracted from magic numbers
ADAPTIVE_CONFIG_DEFAULT_RULES = 7
ADAPTIVE_CONFIG_DEFAULT_RULES_2 = 90
ADAPTIVE_CONFIG_DURATION_BUCKET_MONTH_DAYS = 30
ADAPTATIONS = 30

logger = logging.getLogger(__name__)


class AdaptiveConfig:
    """基于使用统计自动调整系统参数

    设计约束（审计要求）：
    - 单次调整幅度 ≤ 20%
    - 同一配置 24h 内最多调整一次
    - 不触发用户通知
    - 应用后记录到 config_adaptation_log 表，24h 后对比调整前后的 metric，恶化则自动回滚
    """

    # 默认调整规则：{config_key: {metric, threshold_high, threshold_low, adjust_up, adjust_down}}
    DEFAULT_RULES = DEFAULT_ADAPTIVE_POLICY_RULES

    # 调整约束
    MAX_ADJUST_RATIO = 0.20  # 单次调整幅度 ≤ 20%
    COOLDOWN_HOURS = 24  # 同一配置 24h 内最多调整一次
    ROLLBACK_CHECK_HOURS = 24  # 24h 后检查是否恶化

    def __init__(
        self,
        base_config: Dict | None = None,
        ewma_alpha: float = 0.3,
        db_path: Path | None = None,
        policy: EffectivePolicy | None = None,
        initialize: bool = True,
    ):
        runtime_config = None
        # [P2-11] 默认从系统配置加载，避免 base_config 为空导致 _get_config_value 永远返回 None
        if base_config is None:
            try:
                runtime_config = get_config()
                get_value = getattr(runtime_config, "get", None)
                if not callable(get_value):
                    base_config = {}
                else:
                    # 提取可调整的数值配置子集（用于初始化规则 current_value 参考）
                    base_config = {
                        "scoring": {
                            "min_samples_per_dimension": get_value(
                                "scoring.min_samples_per_dimension", 12
                            ),
                        },
                        "distill": {
                            "trigger_threshold": get_value(
                                "distill.trigger_threshold", 0.4
                            ),
                            "min_session_fragment_pass_ratio": get_value(
                                "distill.min_session_fragment_pass_ratio", 0.5
                            ),
                        },
                        "app": {
                            "push_max_items": get_value("app.push_max_items", 3),
                        },
                        "quality_gate": {
                            "base_threshold": get_value(
                                "quality_gate.base_threshold", 0.55
                            ),
                            "review_margin": get_value(
                                "quality_gate.review_margin", 0.15
                            ),
                        },
                        "knowledge_graph": {
                            "freshness_decay_half_life_days": get_value(
                                "knowledge_graph.freshness_decay_half_life_days",
                                ADAPTIVE_CONFIG_DURATION_BUCKET_MONTH_DAYS,
                            ),
                        },
                        "raw_event_store": {
                            "retention_days": get_value(
                                "raw_event_store.retention_days", 30
                            ),
                        },
                        "document_process": {
                            "max_file_size_mb": get_value(
                                "document_process.max_file_size_mb", 100
                            ),
                        },
                        "intent_router": {
                            "llm_fallback_threshold": get_value(
                                "intent_router.llm_fallback_threshold", 0.65
                            ),
                        },
                        "trust": {
                            "min_delivery_score": get_value(
                                "trust.min_delivery_score", 0.55
                            ),
                        },
                    }
            except (OSError, RuntimeError, ValueError, TypeError, KeyError) as exc:
                logger.error("加载系统配置失败，使用空配置: %s", exc)
                base_config = {}
        self.base_config = base_config
        self.policy = policy or get_effective_policy()
        self.adaptations: Dict[str, Dict] = {}
        self.ewma_alpha = ewma_alpha  # EWMA 平滑系数
        self.rules = list(self.DEFAULT_RULES)
        if runtime_config is not None:
            self._load_custom_rules_from_config(runtime_config)
        if db_path is None:
            db_config = runtime_config or get_config()
            self.db_path = db_config.database_dir / "adaptive_config.db"
        else:
            self.db_path = db_path
        if initialize:
            self._init_db()
        self._load_from_db()

    def _init_db(self):
        """初始化 SQLite 数据库，持久化使用指标和调整历史"""
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(str(self.db_path)) as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            # 指标记录表
            conn.execute("""
                CREATE TABLE IF NOT EXISTS usage_metrics (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    feature TEXT NOT NULL,
                    metric TEXT NOT NULL,
                    value REAL NOT NULL,
                    ewma REAL NOT NULL,
                    recorded_at TEXT NOT NULL
                )
            """)
            # 调整历史表（用于审计和回滚）
            conn.execute("""
                CREATE TABLE IF NOT EXISTS config_adaptation_log (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    config_key TEXT NOT NULL,
                    old_value REAL NOT NULL,
                    new_value REAL NOT NULL,
                    reason TEXT,
                    confidence REAL NOT NULL,
                    metric_before REAL,
                    metric_after REAL,
                    rolled_back INTEGER DEFAULT 0,
                    applied_at TEXT NOT NULL,
                    checked_at TEXT
                )
            """)
            conn.commit()

    def _load_from_db(self):
        """从数据库恢复最近的 EWMA 值"""
        if not self.db_path.is_file():
            return
        try:
            with sqlite3.connect(
                self.db_path.resolve().as_uri() + "?mode=ro",
                uri=True,
            ) as conn:
                rows = conn.execute("""
                    SELECT feature, metric, ewma
                    FROM usage_metrics
                    WHERE (feature, metric, recorded_at) IN (
                        SELECT feature, metric, MAX(recorded_at)
                        FROM usage_metrics
                        GROUP BY feature, metric
                    )
                """).fetchall()
            for feature, metric, ewma in rows:
                if feature not in self.adaptations:
                    self.adaptations[feature] = {"metrics": {}, "last_updated": ""}
                self.adaptations[feature]["metrics"][metric] = {
                    "ewma": ewma,
                    "history": deque(maxlen=ADAPTATIONS),
                    "last_value": ewma,
                }
        except (sqlite3.Error, OSError):
            logger.debug("[AdaptiveConfig] 从数据库恢复失败", exc_info=True)

    def _load_custom_rules_from_config(self, runtime_config: Any | None) -> None:
        """Load user-defined adjustment rules from config into the runtime rule set."""
        if runtime_config is None:
            try:
                runtime_config = get_config()
            except (OSError, ValueError, TypeError, AttributeError) as exc:
                logger.warning("[AdaptiveConfig] 自定义规则配置读取失败: %s", exc)
                return

        get_value = getattr(runtime_config, "get", None)
        if not callable(get_value):
            logger.debug("[AdaptiveConfig] runtime config 无 get()，跳过自定义规则加载")
            return

        rules = get_value("adaptive_config.rules", [])
        if not rules:
            return
        if not isinstance(rules, list):
            logger.warning("[AdaptiveConfig] adaptive_config.rules 必须是列表，已忽略")
            return

        required = (
            "config_key",
            "metric",
            "threshold_high",
            "threshold_low",
            "adjust_up",
            "adjust_down",
            "min_value",
            "max_value",
        )
        for index, rule in enumerate(rules):
            if not isinstance(rule, dict):
                logger.warning("[AdaptiveConfig] 自定义规则 %s 不是对象，已忽略", index)
                continue

            missing = [key for key in required if key not in rule]
            if missing:
                logger.warning(
                    "[AdaptiveConfig] 自定义规则 %s 缺少字段 %s，已忽略",
                    index,
                    ",".join(missing),
                )
                continue

            try:
                self.add_rule(
                    config_key=str(rule["config_key"]),
                    metric=str(rule["metric"]),
                    threshold_high=float(rule["threshold_high"]),
                    threshold_low=float(rule["threshold_low"]),
                    adjust_up=float(rule["adjust_up"]),
                    adjust_down=float(rule["adjust_down"]),
                    min_value=float(rule["min_value"]),
                    max_value=float(rule["max_value"]),
                )
            except (TypeError, ValueError) as exc:
                logger.warning("[AdaptiveConfig] 自定义规则 %s 无效: %s", index, exc)

    def _persist_metric(self, feature: str, metric: str, value: float, ewma: float):
        """持久化指标记录到数据库"""
        try:
            with sqlite3.connect(str(self.db_path)) as conn:
                conn.execute(
                    """
                    INSERT INTO usage_metrics (feature, metric, value, ewma, recorded_at)
                    VALUES (?, ?, ?, ?, ?)
                """,
                    (feature, metric, value, ewma, datetime.now().isoformat()),
                )
                conn.commit()
        except (sqlite3.Error, OSError):
            logger.debug("[AdaptiveConfig] 指标持久化失败", exc_info=True)

    def record_usage(self, feature: str, metric: str, value: float):
        """记录功能使用指标"""
        if feature not in self.adaptations:
            self.adaptations[feature] = {
                "metrics": {},
                "last_updated": "",
            }

        feature_data = self.adaptations[feature]

        # EWMA 更新
        if metric not in feature_data["metrics"]:
            feature_data["metrics"][metric] = {
                "ewma": value,
                "history": deque(maxlen=ADAPTIVE_CONFIG_DURATION_BUCKET_MONTH_DAYS),
                "last_value": value,
            }
        else:
            old_ewma = feature_data["metrics"][metric]["ewma"]
            new_ewma = self.ewma_alpha * value + (1 - self.ewma_alpha) * old_ewma
            feature_data["metrics"][metric]["ewma"] = new_ewma
            feature_data["metrics"][metric]["last_value"] = value

        # 保留最近 30 条历史
        feature_data["metrics"][metric]["history"].append(
            {
                "value": value,
                "timestamp": datetime.now().isoformat(),
            }
        )

        feature_data["last_updated"] = datetime.now().isoformat()

        # 持久化到数据库
        self._persist_metric(
            feature,
            metric,
            value,
            feature_data["metrics"][metric]["ewma"],
        )

    def get_ewma(self, feature: str, metric: str) -> float:
        """获取指定指标的 EWMA 值"""
        return self.adaptations.get(feature, {}).get("metrics", {}).get(metric, {}).get("ewma", 0.0)  # type: ignore[no-any-return]  # noqa: E501

    def get_trend(self, feature: str, metric: str) -> str:
        """
        判断趋势方向

        Returns:
            "up" / "down" / "stable"
        """
        history = (
            self.adaptations.get(feature, {}).get("metrics", {}).get(metric, {}).get("history", [])
        )
        if len(history) < 5:
            return "stable"

        recent = [h["value"] for h in history[-5:]]
        earlier = [h["value"] for h in history[:5]]

        recent_avg = sum(recent) / len(recent)
        earlier_avg = sum(earlier) / len(earlier)

        diff = recent_avg - earlier_avg
        threshold = abs(earlier_avg) * 0.1 if earlier_avg != 0 else 0.01

        if diff > threshold:
            return "up"
        elif diff < -threshold:
            return "down"
        return "stable"

    def _is_in_cooldown(self, config_key: str) -> bool:
        """检查配置是否在 24h 冷却期内"""
        try:
            with sqlite3.connect(str(self.db_path)) as conn:
                row = conn.execute(
                    """
                    SELECT applied_at FROM config_adaptation_log
                    WHERE config_key = ? AND rolled_back = 0
                    ORDER BY applied_at DESC LIMIT 1
                """,
                    (config_key,),
                ).fetchone()
            if row:
                last_time = datetime.fromisoformat(row[0])
                return (datetime.now() - last_time) < timedelta(hours=self.COOLDOWN_HOURS)
        except (sqlite3.Error, OSError, ValueError):
            logger.warning(
                "[adaptive_config] (sqlite3.Error, OSError, ValueError) suppressed", exc_info=True
            )
        return False

    def _clamp_adjustment(self, current: float, suggested: float) -> float:
        """限制调整幅度在 ±20% 内"""
        if current == 0:
            max_change = abs(suggested) * self.MAX_ADJUST_RATIO
        else:
            max_change = abs(current) * self.MAX_ADJUST_RATIO
        delta = suggested - current
        if delta > max_change:
            return current + max_change
        elif delta < -max_change:
            return current - max_change
        return suggested

    def suggest_adjustments(self) -> Dict[str, Any]:
        """
        基于使用数据建议配置调整

        约束：
        - 单次调整幅度 ≤ 20%
        - 同一配置 24h 内最多调整一次（冷却期）

        Returns:
            {config_key: {"current": val, "suggested": val, "reason": str, "confidence": float}}
        """
        suggestions = {}

        for rule in self.rules:
            config_key = rule["config_key"]
            metric = rule["metric"]

            # 冷却期检查
            if self._is_in_cooldown(config_key):  # type: ignore[arg-type]
                continue

            # 解析 metric 路径："feature.metric_name"
            parts = metric.split(".")  # type: ignore[attr-defined]
            if len(parts) != 2:
                continue

            feature, metric_name = parts
            ewma_value = self.get_ewma(feature, metric_name)

            if ewma_value == 0.0:
                continue  # 无数据，跳过

            # 获取当前配置值
            current_value = self._get_config_value(config_key)  # type: ignore[arg-type]
            if current_value is None:
                continue

            # 判断是否需要调整
            suggested = None
            reason = None
            confidence = 0.0

            if ewma_value > rule["threshold_high"]:  # type: ignore[operator]
                # 指标过高，需要上调配置
                suggested = current_value + rule["adjust_up"]  # type: ignore[operator]
                reason = f"{metric} EWMA={ewma_value:.3f} > 阈值 {rule['threshold_high']}，建议上调 {config_key}"  # noqa: E501
                confidence = min(
                    1.0,
                    (ewma_value - rule["threshold_high"])  # type: ignore[operator]
                    / rule["threshold_high"],  # type: ignore[operator]
                )
            elif ewma_value < rule["threshold_low"]:  # type: ignore[operator]
                # 指标过低，需要下调配置
                suggested = current_value + rule["adjust_down"]  # type: ignore[operator]
                reason = f"{metric} EWMA={ewma_value:.3f} < 阈值 {rule['threshold_low']}，建议下调 {config_key}"  # noqa: E501
                confidence = min(
                    1.0, (rule["threshold_low"] - ewma_value) / rule["threshold_low"]  # type: ignore[operator]  # noqa: E501
                )  # type: ignore[operator]

            if suggested is not None:
                # 边界限制
                # type: ignore[call-overload]
                suggested = max(rule["min_value"], min(rule["max_value"], suggested))  # type: ignore[call-overload]  # noqa: E501
                # 幅度限制（±20%）
                suggested = self._clamp_adjustment(current_value, suggested)

                # 只有当建议值与当前值差异超过 5% 才建议调整
                if current_value != 0:
                    relative_change = abs(suggested - current_value) / abs(current_value)
                else:
                    relative_change = abs(suggested - current_value)

                if relative_change > 0.05:
                    suggestions[config_key] = {
                        "current": round(current_value, 4),
                        "suggested": round(suggested, 4),
                        "reason": reason,
                        "confidence": round(confidence, 3),
                        "metric": metric,
                        "metric_ewma": round(ewma_value, 4),
                    }

        return suggestions  # type: ignore[return-value]

    def apply_adjustments(self, suggestions: Dict[str, Dict]) -> Dict[str, Any]:
        """应用建议的调整（返回实际应用的结果）

        约束：
        - 只有高置信度 (>0.6) 才自动应用
        - 记录到 config_adaptation_log 表
        - 不触发用户通知
        """
        applied = {}
        for config_key, suggestion in suggestions.items():
            if suggestion.get("confidence", 0) <= 0.6:
                continue
            # 再次检查冷却期（防止竞态）
            if self._is_in_cooldown(config_key):
                continue

            old_value = suggestion["current"]
            new_value = suggestion["suggested"]
            self._set_config_value(config_key, new_value)
            # 同时写入 EffectivePolicy shadow，让决策组件可见
            self.policy.set_shadow(
                config_key,
                new_value,
                experiment_id=config_key,
                metric_before=suggestion.get("metric_ewma"),
            )
            applied[config_key] = new_value

            # 记录到数据库
            try:
                with sqlite3.connect(str(self.db_path)) as conn:
                    conn.execute(
                        """
                        INSERT INTO config_adaptation_log
                        (config_key, old_value, new_value, reason, confidence,
                         metric_before, applied_at)
                        VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                        (
                            config_key,
                            old_value,
                            new_value,
                            suggestion.get("reason", ""),
                            suggestion.get("confidence", 0),
                            suggestion.get("metric_ewma"),
                            datetime.now().isoformat(),
                        ),
                    )
                    conn.commit()
            except (sqlite3.Error, OSError):
                logger.debug("[AdaptiveConfig] 记录调整历史失败", exc_info=True)

            from core.ops.runtime_flow_telemetry import (
                record_runtime_produced,
                runtime_item_id,
            )

            record_runtime_produced(
                "adaptive_config_to_runtime_weights",
                source="core/kia/adaptive_config.py",
                item_id=runtime_item_id("adaptive-policy", config_key),
                intended_consumers=["core/kia/policy.py"],
                metadata={"transition": "policy_shadow_applied", "config_key": config_key},
                config_or_path=self.db_path.parent,
            )

            logger.info(
                "[AdaptiveConfig] 自动应用配置调整: %s %.4f → %.4f (置信度=%.2f)",
                config_key,
                old_value,
                new_value,
                suggestion.get("confidence", 0),
            )
        return applied

    def check_and_rollback(self):
        """检查 24h 前的调整，若 metric 恶化则自动回滚"""
        try:
            cutoff = (datetime.now() - timedelta(hours=self.ROLLBACK_CHECK_HOURS)).isoformat()
            with sqlite3.connect(str(self.db_path)) as conn:
                rows = conn.execute(
                    """
                    SELECT id, config_key, old_value, new_value, metric_before, applied_at
                    FROM config_adaptation_log
                    WHERE checked_at IS NULL AND applied_at < ? AND rolled_back = 0
                """,
                    (cutoff,),
                ).fetchall()

            for row_id, config_key, old_value, new_value, metric_before, applied_at in rows:
                # 获取当前 metric 值
                parts = None
                for rule in self.rules:
                    if rule["config_key"] == config_key:
                        parts = rule["metric"].split(".")
                        break
                if not parts or len(parts) != 2:
                    continue

                feature, metric_name = parts
                current_metric = self.get_ewma(feature, metric_name)

                # 让 EffectivePolicy 根据 metric 决定 commit 或 rollback
                committed = self.policy.commit_or_rollback(config_key, metric_after=current_metric)

                if committed:
                    logger.info(
                        "[AdaptiveConfig] 配置调整验证通过，已提交: %s (metric: %.4f → %.4f)",
                        config_key,
                        metric_before or 0.0,
                        current_metric,
                    )
                else:
                    # Policy 已回滚，同步回 base_config
                    self._set_config_value(config_key, old_value)
                    logger.warning(
                        "[AdaptiveConfig] 配置调整恶化，已回滚: %s %.4f → %.4f"
                        " (metric: %.4f → %.4f)",
                        config_key,
                        new_value,
                        old_value,
                        metric_before or 0.0,
                        current_metric,
                    )

                with sqlite3.connect(str(self.db_path)) as conn:
                    conn.execute(
                        """
                        UPDATE config_adaptation_log
                        SET rolled_back = ?, checked_at = ?, metric_after = ?
                        WHERE id = ?
                    """,
                        (0 if committed else 1, datetime.now().isoformat(), current_metric, row_id),
                    )
                    conn.commit()
        except (sqlite3.Error, OSError):
            logger.debug("[AdaptiveConfig] 回滚检查失败", exc_info=True)

    def _get_config_value(self, key: str) -> Optional[float]:
        """按点号路径获取配置值（优先本实例 base_config，兜底 EffectivePolicy）"""
        keys = key.split(".")
        val: Any = self.base_config
        for k in keys:
            if isinstance(val, dict) and k in val:
                val = val[k]
            else:
                val = None
                break
        if isinstance(val, (int, float)):
            return float(val)

        policy_val = self.policy.get(key)
        if isinstance(policy_val, (int, float)):
            return float(policy_val)
        return None

    def _set_config_value(self, key: str, value: float):
        """按点号路径设置配置值（保持 base_config 视图同步）"""
        keys = key.split(".")
        data = self.base_config
        for k in keys[:-1]:
            if k not in data:
                data[k] = {}
            data = data[k]
        data[keys[-1]] = value

    def refresh_metrics_from_db(self):
        """从数据库重新加载指标 EWMA（用于 daemon 在多个写入源之间同步）。"""
        self._load_from_db()

    def get_effective(self, key: str, default: Any | None = None) -> Any:
        """读取当前有效配置值（代理到 EffectivePolicy）。"""
        return self.policy.get(key, default)

    def get_metrics_summary(self) -> Dict:
        """获取所有指标的汇总"""
        summary = {}  # type: ignore[var-annotated]
        for feature, data in self.adaptations.items():
            summary[feature] = {}
            for metric, metric_data in data.get("metrics", {}).items():
                summary[feature][metric] = {
                    "ewma": round(metric_data["ewma"], 4),
                    "trend": self.get_trend(feature, metric),
                    "last_value": round(metric_data["last_value"], 4),
                    "sample_count": len(metric_data["history"]),
                }
        return summary

    def get_policy_summary(self) -> Dict[str, Any]:
        """Return adaptive policy coverage and active shadow status."""
        report = build_adaptive_policy_report()
        shadows = self.policy.list_shadows()
        active = []
        overdue = 0
        now = datetime.now()
        for config_key, meta in sorted(shadows.items()):
            applied_at = meta.get("applied_at")
            age_hours = None
            needs_decision = False
            if applied_at:
                try:
                    age_hours = (
                        now - datetime.fromisoformat(str(applied_at))
                    ).total_seconds() / 3600
                    needs_decision = age_hours >= self.ROLLBACK_CHECK_HOURS
                except ValueError:
                    age_hours = None
            if needs_decision:
                overdue += 1
            active.append(
                {
                    "config_key": config_key,
                    "experiment_id": meta.get("experiment_id", ""),
                    "old_value": meta.get("old_value"),
                    "new_value": meta.get("new_value"),
                    "metric_before": meta.get("metric_before"),
                    "applied_at": applied_at,
                    "age_hours": round(age_hours, 2) if age_hours is not None else None,
                    "needs_decision": needs_decision,
                }
            )

        return {
            "schema_version": report["schema_version"],
            "ok": bool(report["ok"]) and overdue == 0,
            "coverage_count": report["coverage_count"],
            "rule_count": report["rule_count"],
            "domains": report["domains"],
            "coverage_errors": report["errors"],
            "active_shadow_count": len(active),
            "overdue_shadow_count": overdue,
            "active_shadows": active,
        }

    def add_rule(
        self,
        config_key: str,
        metric: str,
        threshold_high: float,
        threshold_low: float,
        adjust_up: float,
        adjust_down: float,
        min_value: float,
        max_value: float,
    ):
        """添加自定义调整规则"""
        if threshold_low > threshold_high:
            raise ValueError("threshold_low must be <= threshold_high")
        if min_value > max_value:
            raise ValueError("min_value must be <= max_value")
        self.rules.append(
            {
                "config_key": config_key,
                "metric": metric,
                "threshold_high": threshold_high,
                "threshold_low": threshold_low,
                "adjust_up": adjust_up,
                "adjust_down": adjust_down,
                "min_value": min_value,
                "max_value": max_value,
            }
        )
