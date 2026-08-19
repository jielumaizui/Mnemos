# -*- coding: utf-8 -*-
"""
rule_helpers — 从 V1 scorer 迁移到 V2 的规则函数集合

职责：
- 把 V1 五域 scorer 中有价值、无替代的启发式规则沉淀为纯函数。
- 供 AdaptiveScorerV2._rule_score() 调用，不再依赖 V1 scorer 类。

注意：
- 这里只保留“规则”本身，不涉及 ML 模型、训练队列或持久化。
- 所有函数输入尽量是已提取的 features 或原始 content，输出为 [0, 1] 分数。
"""

from __future__ import annotations
import logging

import math
import re
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Union

from core.kia.policy import get_effective_policy

logger = logging.getLogger(__name__)
# Constants extracted from magic numbers
DURATION_BUCKET_MONTH_DAYS = 30
OPS_CAPACITY_RISK_SCORE_PCT = 90
CONTENT = 4096


# ==================== Distill 维度规则 ====================


def distill_value_score(content: str) -> float:
    """蒸馏价值：调用共享 RuleScorer 的加权规则混合评分。

    这是 V1 DistillScorer._distill_rule 的核心逻辑，比 V2 原来只看
    代码块/表格的启发式更丰富。使用共享实例可让 RuleWeightStore
    中的权重立即生效。
    """
    if not content:
        return 0.0
    try:
        from core.kia.rule_scorer import get_shared_rule_scorer

        return float(get_shared_rule_scorer().score(content))
    except (OSError, ValueError, TypeError, KeyError, ImportError, AttributeError, RuntimeError):
        # RuleScorer 不可用时回退到简单启发式
        content.lower()
        score = 0.5
        if "```" in content:
            score += 0.2
        if "|" in content and "\n" in content:
            score += 0.15
        return min(1.0, score)


def falsifiability_score(content: str) -> float:
    """可证伪性：内容包含具体断言/数据/指标时更易验证。

    来自 V1 DistillScorer._falsify_rule。
    """
    if not content:
        return 0.0
    lower = content.lower()
    score = 0.2
    if re.search(r"\d+\.?\d*\s*%", content):
        score += 0.2
    if re.search(r"\d+\s*ms\b", content):
        score += 0.1
    if re.search(r"\d+\.?\d*\s*s\b", content):
        score += 0.1
    assert_words = (
        "必须",
        "一定",
        "never",
        "always",
        "应该",
        "should",
        "必须不",
    )
    hits = sum(1 for w in assert_words if w in lower)
    score += min(0.3, hits * 0.1)
    return min(1.0, score)


def evolution_score(content: str) -> float:
    """进化潜力：涉及技术选型/架构决策的内容未来价值更高。

    来自 V1 DistillScorer._evolve_rule。
    """
    if not content:
        return 0.0
    lower = content.lower()
    score = 0.2
    signals = sum(
        1
        for kw in (
            "架构",
            "设计",
            "选型",
            "迁移",
            "升级",
            "重构",
            "architecture",
            "migration",
            "upgrade",
            "refactor",
            "替代",
            "比较",
            "对比",
            "vs",
        )
        if kw in lower
    )
    return min(1.0, score + signals * 0.15)


def heat_score(content: str, has_code: bool = False) -> float:
    """热度预测：代码 + 决策/方案类词汇表示热知识。

    来自 V1 DistillScorer._heat_rule。
    """
    if not content:
        return 0.0
    score = 0.3
    if has_code:
        score += 0.2
    decision_words = ("决定", "方案", "选择", "decided", "solution")
    if any(kw in content for kw in decision_words):
        score += 0.2
    return min(1.0, score)


# ==================== Sync 维度规则 ====================


def sync_urgency_score(content: str) -> float:
    """同步紧迫度：包含崩溃/异常/故障等关键词则高分。

    替代 V2 原来仅按字数倒序的简单规则，逻辑来自 V1 SyncScorer._urgency_rule。
    """
    if not content:
        return 0.0
    lower = content.lower()
    urgent_signals = sum(
        1
        for kw in (
            "崩溃",
            "异常",
            "error",
            "crash",
            "fatal",
            "紧急",
            "生产环境",
            "线上",
            "outage",
            "down",
            "故障",
        )
        if kw in lower
    )
    return min(1.0, urgent_signals * 0.25 + 0.1)


def sync_noise_score(content: str) -> float:
    """噪声程度：低表示高噪声（应跳过），高表示高质量。

    来自 V1 SyncScorer._noise_rule，复用 rule_scorer.noise_penalty。
    """
    if not content:
        return 0.0
    try:
        from core.kia.rule_scorer import noise_penalty

        return float(noise_penalty(content).score)
    # DEBT(S8): 容错降级，返回默认值避免局部失败扩散
    except (
        OSError, ValueError, TypeError, KeyError, ImportError, AttributeError, RuntimeError
    ):
        return 0.5


def sync_priority_score(
    content: str,
    has_code: bool = False,
    length: int = 0,
    has_list: bool = False,
) -> float:
    """同步优先级：高质量 + 有代码 = 高优先级。

    来自 V1 SyncScorer._priority_rule。
    """
    if not content:
        return 0.0
    score = 0.3
    if has_code:
        score += 0.3
    if (length or len(content)) > 200:
        score += 0.2
    if has_list:
        score += 0.1
    return min(1.0, score)


# ==================== KG 维度规则 ====================


def entity_quality_score(content: str) -> float:
    """实体质量：命名实体/代码块/链接密度。

    来自 V1 KGScorer._entity_quality_rule。
    """
    if not content:
        return 0.0
    try:
        from core.kia.rule_scorer import entity_density_score

        return float(entity_density_score(content).score)
    # DEBT(S8): 容错降级，返回默认值避免局部失败扩散
    except (
        OSError, ValueError, TypeError, KeyError, ImportError, AttributeError, RuntimeError
    ):
        return 0.5


def relation_confidence_score(content: str) -> float:
    """关系置信度：Wiki 引用 + 明确关联词。

    来自 V1 KGScorer._relation_confidence_rule。
    """
    if not content:
        return 0.0
    lower = content.lower()
    score = 0.3
    wiki_refs = re.findall(r"\[\[([^\]]+)\]\]", content)
    if wiki_refs:
        score += min(0.3, len(wiki_refs) * 0.1)
    relation_words = sum(
        1 for kw in ("依赖", "基于", "使用", "depends", "uses", "related") if kw in lower
    )
    score += min(0.2, relation_words * 0.1)
    return min(1.0, score)


def knowledge_freshness_score(
    last_updated: Optional[Union[str, datetime]] = None,
    half_life_days: Optional[int] = None,
) -> float:
    if half_life_days is None:
        half_life_days = get_effective_policy().get(
            "knowledge_graph.freshness_decay_half_life_days", DURATION_BUCKET_MONTH_DAYS
        )
    """知识新鲜度：基于时间衰减。

    来自 V1 KGScorer._freshness_rule。
    """
    if last_updated is None:
        return 0.8
    try:
        if isinstance(last_updated, str):
            last_dt = datetime.fromisoformat(last_updated.replace("Z", "+00:00"))
        else:
            last_dt = last_updated
        days_since = (datetime.now() - last_dt).days
        decay = math.exp(-0.693 * days_since / half_life_days) if days_since > 0 else 1.0
        return max(0.0, min(1.0, decay))
    except (ValueError, TypeError):
        return 0.8


def update_knowledge_freshness(
    current_freshness: float,
    evidence_type: str,  # "confirm" | "contradict" | "neutral"
    days_since_last: int = 0,
    half_life_days: Optional[int] = None,
) -> float:
    if half_life_days is None:
        half_life_days = get_effective_policy().get(
            "knowledge_graph.freshness_decay_half_life_days", DURATION_BUCKET_MONTH_DAYS
        )
    """贝叶斯更新知识新鲜度。

    来自 V1 KGScorer.update_freshness。
    """
    decay = math.exp(-0.693 * days_since_last / half_life_days) if days_since_last > 0 else 1.0
    fresh = current_freshness * decay

    if evidence_type == "confirm":
        fresh = min(1.0, fresh + 0.2)
    elif evidence_type == "contradict":
        fresh *= 0.5

    return max(0.0, min(1.0, fresh))


def entity_decision(score: float, threshold: float = 0.3) -> str:
    """实体入库决策。

    来自 V1 KGScorer.entity_decision。
    """
    if score >= 0.5:
        return "accept"
    elif score >= threshold:
        return "tentative"
    return "reject"


def relation_level(confidence: float, strong: float = 0.7, weak: float = 0.4) -> str:
    """关系置信度等级。

    来自 V1 KGScorer.relation_level。
    """
    if confidence >= strong:
        return "strong"
    elif confidence >= weak:
        return "weak"
    return "suspect"


# ==================== Ops 维度规则 ====================


def ops_anomaly_score(content: str) -> float:
    """运维异常：中英文错误/失败/超时/崩溃关键词。

    增强 V2 ops 维度原来只匹配英文关键词的不足，词表来自 V1 OpsScorer._anomaly_rule。
    """
    if not content:
        return 0.0
    lower = content.lower()
    score = 0.1
    error_signals = sum(
        1
        for kw in (
            "error",
            "fail",
            "timeout",
            "crash",
            "异常",
            "失败",
            "超时",
            "崩溃",
            "拒绝",
            "denied",
            "拒绝连接",
        )
        if kw in lower
    )
    return min(1.0, score + error_signals * 0.2)


def ops_health_score(content: str) -> float:
    """健康分数：成功信号加分，错误信号降分。

    来自 V1 OpsScorer._health_rule。
    """
    if not content:
        return 1.0
    lower = content.lower()
    score = 0.8
    success_signals = sum(
        1
        for kw in (
            "成功",
            "完成",
            "正常",
            "ok",
            "success",
            "healthy",
        )
        if kw in lower
    )
    score += min(0.2, success_signals * 0.05)
    error_signals = sum(1 for kw in ("error", "fail", "异常", "失败") if kw in lower)
    score -= min(0.5, error_signals * 0.15)
    return max(0.0, min(1.0, score))


def ops_capacity_risk_score(content: str) -> float:
    """容量风险：磁盘/内存/连接池告警 = 高风险。

    来自 V1 OpsScorer._capacity_rule。
    """
    if not content:
        return 0.0
    lower = content.lower()
    score = 0.1
    disk_usage = re.search(r"磁盘.*?(\d+)%|disk.*?(\d+)%", content)
    if disk_usage:
        pct = int(disk_usage.group(1) or disk_usage.group(2))
        if pct > OPS_CAPACITY_RISK_SCORE_PCT:
            score += 0.6
        elif pct > 80:
            score += 0.3
    if "连接池满" in content or "connection pool" in lower:
        score += 0.4
    if "oom" in lower or "内存不足" in content or "out of memory" in lower:
        score += 0.5
    return min(1.0, score)


def score_system(log_path: Optional[Union[str, "Path"]]) -> Dict[str, float]:
    """系统级健康评分（daemon 心跳调用）。

    来自 V1 OpsScorer.score_system，读取日志并返回健康/异常/容量三维分数。
    """
    from pathlib import Path

    content = ""
    if log_path is not None:
        path = Path(log_path)
        if path.exists():
            try:
                content = path.read_text(encoding="utf-8", errors="ignore")[-CONTENT:]
            except (OSError, IOError):
                logging.getLogger(__name__).warning(
                    "[rule_helpers] (OSError, IOError) suppressed", exc_info=True
                )
    if not content:
        return {"health_score": 1.0, "anomaly_score": 0.0, "capacity_risk": 0.0}
    return {
        "health_score": ops_health_score(content),
        "anomaly_score": ops_anomaly_score(content),
        "capacity_risk": ops_capacity_risk_score(content),
    }


# ==================== Profile 维度规则 ====================


def profile_behavior_score(content: str, has_code: bool = False, hour_of_day: int = 0) -> float:
    """行为模式强度：重复性/技术栈/时间模式。

    来自 V1 ProfileScorer._behavior_rule。
    """
    if not content:
        return 0.0
    score = 0.3
    if has_code:
        score += 0.2
    if 9 <= hour_of_day <= 18:
        score += 0.1
    return min(1.0, score)


def profile_blind_spot_score(content: str) -> float:
    """盲点分数：提问/探索型内容表示知识盲区大。

    来自 V1 ProfileScorer._blind_spot_rule。
    """
    if not content:
        return 0.0
    lower = content.lower()
    score = 0.2
    question_marks = content.count("?") + content.count("？")
    if question_marks > 2:
        score += 0.3
    explore_words = sum(
        1
        for kw in (
            "怎么",
            "如何",
            "什么",
            "为什么",
            "how",
            "what",
            "why",
            "不了解",
            "不清楚",
            "没见过",
            "第一次",
        )
        if kw in lower
    )
    score += min(0.3, explore_words * 0.1)
    return min(1.0, score)


def profile_stability_score(preference_history: List[str]) -> float:
    """偏好稳定性：重复选择相同技术/方案 = 高稳定性。

    来自 V1 ProfileScorer._stability_rule。
    """
    if not preference_history or len(preference_history) < 2:
        return 0.5
    counts = Counter(preference_history)
    most_common = counts.most_common(1)[0][1]
    repeat_ratio = most_common / len(preference_history)
    history_bonus = min(0.2, len(preference_history) * 0.02)
    score = 0.3 + repeat_ratio * 0.5 + history_bonus
    return min(1.0, score)
