# -*- coding: utf-8 -*-
"""
OpsScorer — 运维层评分器

维度：
  - anomaly_score: 异常分数（0-1，高=异常）
  - health_score: 健康分数（0-1，高=健康）
  - capacity_risk: 容量风险（0-1，高=风险大）
"""

from __future__ import annotations

from typing import Dict, List

from core.scoring.adaptive_scorer import AdaptiveScorer, ScoreCard


class OpsScorer:
    """运维层评分器"""

    def __init__(self):
        self._scorer = AdaptiveScorer(
            domain="ops",
            cold_start_rules={
                "anomaly_score": self._anomaly_rule,
                "health_score": self._health_rule,
                "capacity_risk": self._capacity_rule,
            },
        )

    def score(self, content: str, **kwargs) -> List[ScoreCard]:
        return self._scorer.score(content, dimensions=[
            "anomaly_score", "health_score", "capacity_risk",
        ])

    def score_system(self) -> Dict[str, float]:
        """系统级健康评分（daemon 心跳调用）

        读取 daemon 日志并评分；若日志不可读则返回保守默认值。
        """
        from core.config import get_config
        log_path = get_config().data_dir.parent / "daemon.log"
        content = ""
        if log_path.exists():
            try:
                content = log_path.read_text(encoding="utf-8", errors="ignore")[-4096:]
            except Exception:
                pass
        cards = self.score(content) if content else []
        return {
            "health_score": next((c.value for c in cards if c.dimension == "health_score"), 1.0),
            "anomaly_score": next((c.value for c in cards if c.dimension == "anomaly_score"), 0.0),
            "capacity_risk": next((c.value for c in cards if c.dimension == "capacity_risk"), 0.0),
        }

    def _anomaly_rule(self, features: Dict) -> float:
        """异常分数规则：错误/失败/超时 = 高异常"""
        content = features.get("content", "").lower()
        score = 0.1
        error_signals = sum(1 for kw in (
            "error", "fail", "timeout", "crash", "异常", "失败", "超时",
            "崩溃", "拒绝", "denied", "拒绝连接",
        ) if kw in content)
        return min(1.0, score + error_signals * 0.2)

    def _health_rule(self, features: Dict) -> float:
        """健康分数规则：与异常分数互补"""
        content = features.get("content", "").lower()
        score = 0.8
        # 成功信号
        success_signals = sum(1 for kw in (
            "成功", "完成", "正常", "ok", "success", "healthy",
        ) if kw in content)
        score += min(0.2, success_signals * 0.05)
        # 错误信号降分
        error_signals = sum(1 for kw in ("error", "fail", "异常", "失败") if kw in content)
        score -= min(0.5, error_signals * 0.15)
        return max(0.0, min(1.0, score))

    def _capacity_rule(self, features: Dict) -> float:
        """容量风险规则：磁盘/内存/连接数告警 = 高风险"""
        content = features.get("content", "").lower()
        score = 0.1
        import re
        # 磁盘使用率
        disk_usage = re.search(r'磁盘.*?(\d+)%|disk.*?(\d+)%', content)
        if disk_usage:
            pct = int(disk_usage.group(1) or disk_usage.group(2))
            if pct > 90:
                score += 0.6
            elif pct > 80:
                score += 0.3
        # 连接数
        if "连接池满" in content or "connection pool" in content:
            score += 0.4
        # 内存
        if "oom" in content or "内存不足" in content or "out of memory" in content:
            score += 0.5
        return min(1.0, score)
