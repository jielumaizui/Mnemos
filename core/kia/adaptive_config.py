"""
AdaptiveConfig — 自适应配置

【E14 全库修复】E8 知识轨迹完整实现。
根据使用统计自动调整系统参数。
"""

import json
from typing import Dict, Optional, List
from datetime import datetime, timedelta
import math


class AdaptiveConfig:
    """基于使用统计自动调整系统参数"""

    # 默认调整规则：{config_key: {metric, threshold_high, threshold_low, adjust_up, adjust_down}}
    DEFAULT_RULES = [
        {
            "config_key": "scoring.retrain_buffer",
            "metric": "scoring.feedback_rate",
            "threshold_high": 0.8,
            "threshold_low": 0.2,
            "adjust_up": 50,
            "adjust_down": -50,
            "min_value": 20,
            "max_value": 500,
        },
        {
            "config_key": "distill.trigger_threshold",
            "metric": "distill.false_positive_rate",
            "threshold_high": 0.3,
            "threshold_low": 0.05,
            "adjust_up": 0.05,
            "adjust_down": -0.05,
            "min_value": 0.1,
            "max_value": 0.8,
        },
        {
            "config_key": "app.push_max_items",
            "metric": "app.push_ignore_rate",
            "threshold_high": 0.5,
            "threshold_low": 0.1,
            "adjust_up": 1,
            "adjust_down": -1,
            "min_value": 1,
            "max_value": 10,
        },
        {
            "config_key": "knowledge_graph.freshness_decay_half_life_days",
            "metric": "knowledge_graph.stale_page_rate",
            "threshold_high": 0.4,
            "threshold_low": 0.1,
            "adjust_up": 7,
            "adjust_down": -7,
            "min_value": 7,
            "max_value": 90,
        },
    ]

    def __init__(self, base_config: Dict = None, ewma_alpha: float = 0.3):
        self.base_config = base_config or {}
        self.adaptations: Dict[str, Dict] = {}
        self.ewma_alpha = ewma_alpha  # EWMA 平滑系数
        self.rules = list(self.DEFAULT_RULES)

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
                "history": [],
                "last_value": value,
            }
        else:
            old_ewma = feature_data["metrics"][metric]["ewma"]
            new_ewma = self.ewma_alpha * value + (1 - self.ewma_alpha) * old_ewma
            feature_data["metrics"][metric]["ewma"] = new_ewma
            feature_data["metrics"][metric]["last_value"] = value

        # 保留最近 30 条历史
        feature_data["metrics"][metric]["history"].append({
            "value": value,
            "timestamp": datetime.now().isoformat(),
        })
        if len(feature_data["metrics"][metric]["history"]) > 30:
            feature_data["metrics"][metric]["history"] = feature_data["metrics"][metric]["history"][-30:]

        feature_data["last_updated"] = datetime.now().isoformat()

    def get_ewma(self, feature: str, metric: str) -> float:
        """获取指定指标的 EWMA 值"""
        return self.adaptations.get(feature, {}).get("metrics", {}).get(metric, {}).get("ewma", 0.0)

    def get_trend(self, feature: str, metric: str) -> str:
        """
        判断趋势方向

        Returns:
            "up" / "down" / "stable"
        """
        history = self.adaptations.get(feature, {}).get("metrics", {}).get(metric, {}).get("history", [])
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

    def suggest_adjustments(self) -> Dict[str, any]:
        """
        基于使用数据建议配置调整

        Returns:
            {config_key: {"current": val, "suggested": val, "reason": str, "confidence": float}}
        """
        suggestions = {}

        for rule in self.rules:
            config_key = rule["config_key"]
            metric = rule["metric"]

            # 解析 metric 路径："feature.metric_name"
            parts = metric.split(".")
            if len(parts) != 2:
                continue

            feature, metric_name = parts
            ewma_value = self.get_ewma(feature, metric_name)

            if ewma_value == 0.0:
                continue  # 无数据，跳过

            # 获取当前配置值
            current_value = self._get_config_value(config_key)
            if current_value is None:
                continue

            # 判断是否需要调整
            suggested = None
            reason = None
            confidence = 0.0

            if ewma_value > rule["threshold_high"]:
                # 指标过高，需要上调配置
                suggested = current_value + rule["adjust_up"]
                reason = f"{metric} EWMA={ewma_value:.3f} > 阈值 {rule['threshold_high']}，建议上调 {config_key}"
                confidence = min(1.0, (ewma_value - rule["threshold_high"]) / rule["threshold_high"])
            elif ewma_value < rule["threshold_low"]:
                # 指标过低，需要下调配置
                suggested = current_value + rule["adjust_down"]
                reason = f"{metric} EWMA={ewma_value:.3f} < 阈值 {rule['threshold_low']}，建议下调 {config_key}"
                confidence = min(1.0, (rule["threshold_low"] - ewma_value) / rule["threshold_low"])

            if suggested is not None:
                # 边界限制
                suggested = max(rule["min_value"], min(rule["max_value"], suggested))

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

        return suggestions

    def apply_adjustments(self, suggestions: Dict[str, Dict]) -> Dict[str, any]:
        """应用建议的调整（返回实际应用的结果）"""
        applied = {}
        for config_key, suggestion in suggestions.items():
            if suggestion.get("confidence", 0) > 0.6:  # 只有高置信度才自动应用
                self._set_config_value(config_key, suggestion["suggested"])
                applied[config_key] = suggestion["suggested"]
        return applied

    def _get_config_value(self, key: str) -> Optional[float]:
        """按点号路径获取配置值"""
        keys = key.split(".")
        val = self.base_config
        for k in keys:
            if isinstance(val, dict) and k in val:
                val = val[k]
            else:
                return None
        return float(val) if isinstance(val, (int, float)) else None

    def _set_config_value(self, key: str, value: float):
        """按点号路径设置配置值"""
        keys = key.split(".")
        data = self.base_config
        for k in keys[:-1]:
            if k not in data:
                data[k] = {}
            data = data[k]
        data[keys[-1]] = value

    def get_metrics_summary(self) -> Dict:
        """获取所有指标的汇总"""
        summary = {}
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

    def add_rule(self, config_key: str, metric: str,
                 threshold_high: float, threshold_low: float,
                 adjust_up: float, adjust_down: float,
                 min_value: float, max_value: float):
        """添加自定义调整规则"""
        self.rules.append({
            "config_key": config_key,
            "metric": metric,
            "threshold_high": threshold_high,
            "threshold_low": threshold_low,
            "adjust_up": adjust_up,
            "adjust_down": adjust_down,
            "min_value": min_value,
            "max_value": max_value,
        })
