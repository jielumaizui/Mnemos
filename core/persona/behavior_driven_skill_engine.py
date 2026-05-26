"""
BehaviorDrivenSkillEngine — 行为驱动 Skill 引擎

【E14 全库修复】E18 Skill 飞轮完整实现。
基于用户行为模式驱动 Skill 的演化：
- 行为序列频繁模式挖掘
- Skill 效用评估
- Skill 生成/优化/淘汰建议
"""

import re
from typing import List, Dict, Optional, Tuple
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import datetime


@dataclass
class BehaviorPattern:
    """行为模式"""
    pattern: Tuple[str, ...]        # 动作序列
    frequency: int
    support: float                  # 支持度（出现频率 / 总序列数）
    avg_duration_ms: float
    success_rate: float


@dataclass
class SkillSuggestion:
    """Skill 建议"""
    action: str                     # "create" / "optimize" / "deprecate"
    skill_name: str
    reason: str
    confidence: float
    evidence: List[str]


class BehaviorDrivenSkillEngine:
    """分析用户行为，驱动 Skill 的生成、优化和淘汰"""

    def __init__(self, min_support: float = 0.05, max_pattern_length: int = 4):
        self.min_support = min_support
        self.max_pattern_length = max_pattern_length
        self.patterns: List[BehaviorPattern] = []

    def analyze_behavior(self, actions: List[Dict]) -> List[BehaviorPattern]:
        """
        分析用户行为序列，提取模式

        Args:
            actions: [
                {"action": str, "target": str, "timestamp": str,
                 "context": str, "success": bool, "duration_ms": int}
            ]

        Returns:
            行为模式列表
        """
        if not actions or len(actions) < 3:
            return []

        # 1. 构建动作序列
        action_sequence = [a["action"] for a in actions]
        total_sequences = len(action_sequence)

        # 2. 频繁模式挖掘（bigram 到 n-gram）
        raw_patterns = Counter()
        pattern_metadata = defaultdict(lambda: {"durations": [], "successes": 0, "count": 0})

        for length in range(2, min(self.max_pattern_length + 1, len(action_sequence) + 1)):
            for i in range(len(action_sequence) - length + 1):
                pattern = tuple(action_sequence[i:i + length])
                raw_patterns[pattern] += 1

                # 收集元数据
                durations = [actions[j].get("duration_ms", 0) for j in range(i, i + length)]
                successes = sum(1 for j in range(i, i + length) if actions[j].get("success", True))

                pattern_metadata[pattern]["durations"].extend(durations)
                pattern_metadata[pattern]["successes"] += successes
                pattern_metadata[pattern]["count"] += length

        # 3. 筛选高频模式
        patterns = []
        for pattern, freq in raw_patterns.items():
            support = freq / total_sequences
            if support < self.min_support:
                continue

            meta = pattern_metadata[pattern]
            avg_duration = (sum(meta["durations"]) / len(meta["durations"])
                           if meta["durations"] else 0)
            success_rate = meta["successes"] / meta["count"] if meta["count"] > 0 else 1.0

            patterns.append(BehaviorPattern(
                pattern=pattern,
                frequency=freq,
                support=round(support, 4),
                avg_duration_ms=round(avg_duration, 1),
                success_rate=round(success_rate, 3),
            ))

        # 按支持度排序
        patterns.sort(key=lambda p: p.support, reverse=True)
        self.patterns = patterns
        return patterns

    def suggest_skill_updates(self, current_skills: List[str],
                              actions: List[Dict] = None) -> List[SkillSuggestion]:
        """基于行为分析建议 Skill 更新"""
        suggestions = []

        if actions:
            patterns = self.analyze_behavior(actions)
        else:
            patterns = self.patterns

        if not patterns:
            return suggestions

        # 1. 建议创建新 Skill（高频重复模式）
        for pattern in patterns[:5]:
            if pattern.support >= 0.15 and pattern.success_rate >= 0.8:
                pattern_name = " → ".join(pattern.pattern)
                suggested_skill = self._pattern_to_skill_name(pattern.pattern)

                if suggested_skill not in current_skills:
                    suggestions.append(SkillSuggestion(
                        action="create",
                        skill_name=suggested_skill,
                        reason=(f"检测到高频行为模式（支持度 {pattern.support:.1%}，"
                                f"成功率 {pattern.success_rate:.1%}）：{pattern_name}"),
                        confidence=min(1.0, pattern.support * 3),
                        evidence=[pattern_name, f"出现 {pattern.frequency} 次"],
                    ))

        # 2. 建议优化现有 Skill（成功率低的模式）
        for pattern in patterns:
            if pattern.success_rate < 0.5 and pattern.frequency >= 3:
                pattern_name = " → ".join(pattern.pattern)
                for skill in current_skills:
                    if any(p in skill.lower() for p in pattern.pattern):
                        suggestions.append(SkillSuggestion(
                            action="optimize",
                            skill_name=skill,
                            reason=(f"相关行为模式成功率低（{pattern.success_rate:.1%}）："
                                    f"{pattern_name}"),
                            confidence=min(1.0, (1.0 - pattern.success_rate) * 1.5),
                            evidence=[f"失败率: {1.0 - pattern.success_rate:.1%}",
                                      f"平均耗时: {pattern.avg_duration_ms:.0f}ms"],
                        ))
                        break

        # 3. 建议淘汰 Skill（长时间未使用的）
        if actions:
            skill_usage = self._analyze_skill_usage(current_skills, actions)
            for skill, usage in skill_usage.items():
                if usage["last_used_days"] > 90 and usage["total_uses"] < 5:
                    suggestions.append(SkillSuggestion(
                        action="deprecate",
                        skill_name=skill,
                        reason=(f"Skill 已 {usage['last_used_days']:.0f} 天未使用，"
                                f"总使用次数仅 {usage['total_uses']} 次"),
                        confidence=min(1.0, usage["last_used_days"] / 180),
                        evidence=[f"最后使用: {usage['last_used']}",
                                  f"总使用: {usage['total_uses']} 次"],
                    ))

        # 去重
        seen = set()
        unique = []
        for s in suggestions:
            key = (s.action, s.skill_name)
            if key not in seen:
                seen.add(key)
                unique.append(s)

        return sorted(unique, key=lambda x: x.confidence, reverse=True)

    def _pattern_to_skill_name(self, pattern: Tuple[str, ...]) -> str:
        """将行为模式转换为 Skill 名称建议"""
        # 取前两个动作组合
        parts = list(pattern)[:2]
        return f"auto_{'_'.join(p.lower().replace(' ', '_') for p in parts)}"

    def _analyze_skill_usage(self, skills: List[str],
                             actions: List[Dict]) -> Dict[str, Dict]:
        """分析每个 Skill 的使用情况"""
        usage = defaultdict(lambda: {"total_uses": 0, "last_used": None,
                                      "last_used_days": 999})

        now = datetime.now()

        for action in actions:
            action_name = action.get("action", "")
            timestamp = action.get("timestamp", "")

            for skill in skills:
                if skill.lower() in action_name.lower() or action_name.lower() in skill.lower():
                    usage[skill]["total_uses"] += 1
                    try:
                        action_time = datetime.fromisoformat(timestamp.replace('Z', '+00:00'))
                        days_ago = (now - action_time).days
                        if usage[skill]["last_used"] is None or days_ago < usage[skill]["last_used_days"]:
                            usage[skill]["last_used"] = timestamp
                            usage[skill]["last_used_days"] = days_ago
                    except Exception:
                        pass

        return dict(usage)

    def get_behavior_summary(self, actions: List[Dict]) -> Dict:
        """获取行为分析汇总"""
        if not actions:
            return {"total_actions": 0, "unique_actions": 0, "top_patterns": []}

        action_counts = Counter(a["action"] for a in actions)
        success_count = sum(1 for a in actions if a.get("success", True))

        patterns = self.analyze_behavior(actions)

        return {
            "total_actions": len(actions),
            "unique_actions": len(action_counts),
            "success_rate": round(success_count / len(actions), 3),
            "top_actions": action_counts.most_common(5),
            "top_patterns": [
                {
                    "pattern": " → ".join(p.pattern),
                    "frequency": p.frequency,
                    "support": p.support,
                    "success_rate": p.success_rate,
                }
                for p in patterns[:5]
            ],
        }
