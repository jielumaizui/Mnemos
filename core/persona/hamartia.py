"""
Blind Spot Analyzer - 盲区画像分析器

职责：
- 检测用户的四类盲区（框架/选项/时间/偏好僵化）
- 管理挑战平衡（什么时候迎合、什么时候挑战）
- 记录挑战反馈，校准盲区画像
- 生成"反向视角"建议

核心原则：
- 不是"抬杠"，是"补全视角"
- 挑战必须有数据支撑，不能凭空猜测
- 用户反馈（接受/忽略/拒绝）是盲区画像的核心输入
"""
# Hamartia — 悲剧性缺陷 — 盲点分析，认知盲区识别
# 原模块: blindspot_analyzer.py



import json
import re
from typing import Dict, List, Optional, Any, Tuple
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from collections import Counter

from .psyche import SignalStore, get_signal_store
from .pythia import PreferenceProfile
import logging

logger = logging.getLogger(__name__)


# ========== 数据类 ==========

@dataclass
class BlindSpot:
    """单个盲区条目"""
    type: str                           # framing/option_gap/temporal/preference_rigidity
    description: str                    # 盲区描述（行为语言，非诊断语言）
    evidence: List[str]                 # 支撑证据列表
    confidence: float = 0.0             # 置信度 0-1
    first_detected: str = ""            # ISO时间
    last_challenged: str = ""           # ISO时间
    challenge_count: int = 0            # 挑战次数
    user_reaction: str = ""             # accepted/ignored/rejected
    status: str = "suspected"           # suspected/confirmed/dismissed


@dataclass
class ChallengeRecord:
    """挑战记录"""
    id: str
    timestamp: str
    session_id: str
    blindspot_type: str
    challenge_message: str
    user_reaction: str = ""             # accepted/ignored/rejected
    outcome: str = ""                   # 用户实际行为变化
    challenge_credit_cost: float = 1.0  # 消耗的信用额度


@dataclass
class BlindSpotProfile:
    """盲区画像"""
    confirmed: List[BlindSpot] = field(default_factory=list)
    suspected: List[BlindSpot] = field(default_factory=list)
    dismissed: List[BlindSpot] = field(default_factory=list)

    # 挑战统计
    total_challenges: int = 0
    accepted_count: int = 0
    ignored_count: int = 0
    rejected_count: int = 0
    acceptance_rate: float = 0.0

    # 信用系统
    challenge_credit: float = 10.0      # 当前信用额度
    credit_max: float = 10.0            # 最大额度
    credit_recovery_rate: float = 1.0   # 每天恢复速度

    generated_at: str = ""


# ========== 盲区检测引擎 ==========

class BlindSpotDetector:
    """盲区检测引擎"""

    # 检测规则定义
    DETECTION_RULES = {
        "framing": {
            "name": "框架盲区",
            "description": "用户在隐含假设下做选择，没有意识到问题空间本身可以被质疑",
            "signals": [
                "options_all_share_same_premise",    # 所有选项共享同一前提
                "no_framework_questioning",          # 用户从未质疑问题本身
                "historical_framing_shifts",         # 历史上用户曾被点出框架问题
            ],
        },
        "option_gap": {
            "name": "选项盲区",
            "description": "用户未意识到存在完全未考虑的替代方案",
            "signals": [
                "only_two_options_presented",        # 只呈现了两个选项
                "historical_third_options",          # 历史上同类决策有第三选项
                "no_exploration_behavior",           # 用户没有探索行为
            ],
        },
        "temporal": {
            "name": "时间盲区",
            "description": "用户过度关注短期效果，忽略长期影响和累积效应",
            "signals": [
                "all_options_short_term",            # 所有选项都是短期方案
                "no_maintenance_consideration",      # 不考虑维护成本
                "historical_long_term_issues",       # 历史上有长期问题未考虑
            ],
        },
        "preference_rigidity": {
            "name": "偏好僵化",
            "description": "用户被近期习惯绑架，没有考虑情境变化",
            "signals": [
                "same_choice_pattern",               # 连续多次做相同选择
                "context_changed_but_choice_didnt",  # 情境变了但选择没变
                "no_deviation_from_baseline",        # 从未偏离基线偏好
            ],
        },
    }

    def __init__(self, store: SignalStore = None):
        self.store = store or get_signal_store()

    def detect(self, session_context: Dict, user_options: List[Dict],
               persona: PreferenceProfile, history: BlindSpotProfile) -> List[BlindSpot]:
        """
        检测当前决策场景中的盲区。

        Args:
            session_context: 当前session上下文
            user_options: 用户正在考虑的选项列表
            persona: 当前用户画像
            history: 历史盲区画像

        Returns:
            检测到的盲区列表（按置信度排序）
        """
        blindspots = []

        # 1. 框架盲区检测
        framing = self._detect_framing_blindspot(session_context, user_options, history)
        if framing:
            blindspots.append(framing)

        # 2. 选项盲区检测
        option_gap = self._detect_option_gap(session_context, user_options, persona, history)
        if option_gap:
            blindspots.append(option_gap)

        # 3. 时间盲区检测
        temporal = self._detect_temporal_blindspot(session_context, user_options, history)
        if temporal:
            blindspots.append(temporal)

        # 4. 偏好僵化检测
        rigidity = self._detect_preference_rigidity(session_context, user_options, persona, history)
        if rigidity:
            blindspots.append(rigidity)

        # 按置信度排序
        blindspots.sort(key=lambda x: x.confidence, reverse=True)
        return blindspots

    def _detect_framing_blindspot(self, session_context, user_options, history) -> Optional[BlindSpot]:
        """检测框架盲区"""
        if len(user_options) < 2:
            return None

        # 提取所有选项的前提假设
        premises = []
        for opt in user_options:
            premise = opt.get("premise", "")
            if premise:
                premises.append(premise)

        if not premises:
            return None

        # 检查是否所有选项共享同一前提
        unique_premises = set(premises)
        if len(unique_premises) > 1:
            return None  # 选项有不同的前提，框架可能是多元的

        # 检查历史上是否有点出过框架问题
        historical_framing = [b for b in history.confirmed + history.suspected
                              if b.type == "framing"]

        confidence = 0.5
        if historical_framing:
            confidence += 0.2  # 历史上确实有过框架盲区

        # 如果选项都很相似（只在同一维度上变化）
        similarity = self._calculate_options_similarity(user_options)
        if similarity > 0.7:
            confidence += 0.2

        if confidence < 0.6:
            return None

        return BlindSpot(
            type="framing",
            description=f"你的{len(user_options)}个选项都基于同一个前提「{list(unique_premises)[0]}」，但没有考虑是否可以跳出这个前提",
            evidence=[
                f"所有选项共享前提: {list(unique_premises)[0]}",
                f"选项相似度: {similarity:.2f}",
            ] + ([f"历史上曾被点出框架盲区"] if historical_framing else []),
            confidence=min(1.0, confidence),
            first_detected=datetime.now().isoformat(),
        )

    def _detect_option_gap(self, session_context, user_options, persona, history) -> Optional[BlindSpot]:
        """检测选项盲区"""
        if len(user_options) > 3:
            return None  # 选项已经很多，不太可能是选项盲区

        # 根据任务类型，获取"通常应该有"的选项数
        task_type = session_context.get("task_type", "")
        typical_option_count = self._get_typical_option_count(task_type)

        if len(user_options) >= typical_option_count:
            return None

        # 检查历史上是否有第三选项的模式
        historical_options = self._get_historical_option_patterns(session_context)

        confidence = 0.5
        if historical_options:
            avg_options = sum(historical_options) / len(historical_options)
            if avg_options > len(user_options):
                confidence += 0.2
                evidence = f"历史上同类决策平均有{avg_options:.1f}个选项"
            else:
                return None
        else:
            evidence = f"同类决策通常有{typical_option_count}个维度，你只覆盖了{len(user_options)}个"

        if confidence < 0.6:
            return None

        return BlindSpot(
            type="option_gap",
            description=f"你目前只考虑了{len(user_options)}个选项，但这类决策通常还有你没覆盖的维度",
            evidence=[evidence],
            confidence=min(1.0, confidence),
            first_detected=datetime.now().isoformat(),
        )

    def _detect_temporal_blindspot(self, session_context, user_options, history) -> Optional[BlindSpot]:
        """检测时间盲区"""
        # 检查是否所有选项都是短期方案
        short_term_count = 0
        for opt in user_options:
            time_horizon = opt.get("time_horizon", "")
            if time_horizon in ["immediate", "short", "this_week", "this_month"]:
                short_term_count += 1

        if short_term_count < len(user_options):
            return None  # 至少有一个选项考虑了长期

        # 检查历史上是否有过长期问题
        historical_temporal = [b for b in history.confirmed + history.suspected
                                if b.type == "temporal"]

        confidence = 0.6
        if historical_temporal:
            confidence += 0.15

        # 如果是技术决策，时间盲区更常见
        task_type = session_context.get("task_type", "")
        if "coding" in task_type or "architecture" in task_type:
            confidence += 0.1

        return BlindSpot(
            type="temporal",
            description="你的选项都解决了眼前的问题，但没有覆盖6个月后的维护成本和扩展性",
            evidence=[
                f"{len(user_options)}个选项都是短期导向",
            ] + ([f"历史上曾被点出时间盲区"] if historical_temporal else []),
            confidence=min(1.0, confidence),
            first_detected=datetime.now().isoformat(),
        )

    def _detect_preference_rigidity(self, session_context, user_options, persona, history) -> Optional[BlindSpot]:
        """检测偏好僵化"""
        # 获取最近的选择模式
        recent_selections = self._get_recent_selections(session_context)
        if len(recent_selections) < 3:
            return None

        # 检查是否连续选择同一类型的选项
        option_types = [s.get("option_type", "") for s in recent_selections]
        if not option_types:
            return None

        most_common = Counter(option_types).most_common(1)[0]
        if most_common[1] < len(recent_selections) * 0.7:
            return None  # 选择不够一致

        # 检查当前情境是否与历史不同
        current_context = session_context.get("context_hash", "")
        historical_contexts = [s.get("context_hash", "") for s in recent_selections]
        context_changed = current_context not in historical_contexts

        confidence = 0.5
        if context_changed:
            confidence += 0.2  # 情境变了但选择没变，更可能是僵化

        # 检查基线画像是否有这个偏好
        # 如果基线画像没有这个强偏好，但最近选择很一致，更可能是僵化
        # （简化版：如果连续5次都选了同一种，且不是基线偏好，标记为僵化）
        if len(recent_selections) >= 5:
            confidence += 0.15

        if confidence < 0.6:
            return None

        return BlindSpot(
            type="preference_rigidity",
            description=f"你最近{len(recent_selections)}次同类决策都选了「{most_common[0]}」路线，但这次的情境可能适合不同的选择",
            evidence=[
                f"连续{len(recent_selections)}次选择一致",
                f"最频繁选择: {most_common[0]} ({most_common[1]}次)",
            ] + (["当前情境与历史不同"] if context_changed else []),
            confidence=min(1.0, confidence),
            first_detected=datetime.now().isoformat(),
        )

    # ---- 辅助方法 ----

    def _calculate_options_similarity(self, options: List[Dict]) -> float:
        """计算选项之间的相似度"""
        if len(options) < 2:
            return 1.0

        # 简化：基于共享关键词计算
        all_keywords = []
        for opt in options:
            keywords = set(opt.get("keywords", []))
            all_keywords.append(keywords)

        # 计算两两交集
        intersections = []
        for i in range(len(all_keywords)):
            for j in range(i + 1, len(all_keywords)):
                union = all_keywords[i] | all_keywords[j]
                if union:
                    intersection = all_keywords[i] & all_keywords[j]
                    intersections.append(len(intersection) / len(union))

        return sum(intersections) / len(intersections) if intersections else 0.0

    def _get_typical_option_count(self, task_type: str) -> int:
        """获取某类任务通常的选项数"""
        # 基于经验值的映射
        mapping = {
            "coding": 3,
            "architecture": 4,
            "decision": 3,
            "strategy": 4,
            "analysis": 3,
            "general": 3,
        }
        for key in mapping:
            if key in task_type.lower():
                return mapping[key]
        return 3

    def _get_historical_option_patterns(self, session_context) -> List[int]:
        """获取历史上同类决策的选项数"""
        # 简化版：从数据库查询
        # 实际实现需要更复杂的查询
        return []

    def _get_recent_selections(self, session_context) -> List[Dict]:
        """获取最近的选择记录"""
        # 简化版：从数据库查询
        # 实际实现需要从session_signals中查询
        return []


# ========== 挑战平衡器 ==========

class ChallengeBalancer:
    """
    挑战平衡器：决定什么时候迎合，什么时候挑战

    原则：
    - 挑战是「信用」，不是「义务」
    - 用户接受挑战 → 信用增加
    - 用户拒绝/忽略挑战 → 信用减少
    - 信用耗尽 → 闭嘴
    """

    def __init__(self, profile: BlindSpotProfile = None):
        self.profile = profile or BlindSpotProfile()

    def should_challenge(self, session_context: Dict, blindspots: List[BlindSpot]) -> Tuple[bool, List[BlindSpot], str]:
        """
        决定是否挑战，以及挑战哪些盲区。

        Returns:
            (是否挑战, 挑战列表, 理由)
        """
        # 0. 检查信用额度
        if self.profile.challenge_credit <= 0:
            return False, [], "挑战信用额度已耗尽"

        # 1. 高stakes决策 → 必须挑战
        if session_context.get("decision_risk") == "high":
            return True, blindspots[:2], "高stakes决策，必须提供反向视角"

        # 2. 用户主动要求挑毛病
        user_message = session_context.get("user_message", "").lower()
        challenge_keywords = ["漏洞", "盲区", "没想到", "还有吗", "挑毛病", "反向", "反面"]
        if any(kw in user_message for kw in challenge_keywords):
            return True, blindspots, "用户主动要求挑战"

        # 3. 执行模式 + 时间紧 → 不挑战或轻挑战
        if session_context.get("mode") == "execution" and session_context.get("time_pressure"):
            return False, [], "执行模式且时间紧，优先推进"

        # 4. 用户最近高拒绝率 → 减少挑战
        if self.profile.rejected_count > 0:
            rejection_rate = self.profile.rejected_count / max(self.profile.total_challenges, 1)
            if rejection_rate > 0.7 and self.profile.total_challenges >= 5:
                return False, [], "用户近期拒绝率高，收敛挑战"

        # 5. 默认：只挑战最显著的1-2个盲区
        if blindspots:
            significant = [b for b in blindspots if b.confidence >= 0.7]
            if significant:
                # 消耗信用
                cost = len(significant[:2]) * 1.0
                self.profile.challenge_credit -= cost
                return True, significant[:2], "默认策略：挑战最显著的盲区"

        return False, [], "无显著盲区"

    def record_reaction(self, challenge_id: str, reaction: str, outcome: str = ""):
        """记录用户对挑战的反应"""
        self.profile.total_challenges += 1

        if reaction == "accepted":
            self.profile.accepted_count += 1
            self.profile.challenge_credit = min(
                self.profile.credit_max,
                self.profile.challenge_credit + 2.0  # 接受挑战，信用+2
            )
        elif reaction == "ignored":
            self.profile.ignored_count += 1
            self.profile.challenge_credit -= 0.5  # 忽略，轻微扣信用
        elif reaction == "rejected":
            self.profile.rejected_count += 1
            self.profile.challenge_credit -= 1.5  # 拒绝，扣信用

        # 更新接受率
        total = self.profile.total_challenges
        self.profile.acceptance_rate = self.profile.accepted_count / max(total, 1)

    def recover_credit(self):
        """每日信用恢复"""
        self.profile.challenge_credit = min(
            self.profile.credit_max,
            self.profile.challenge_credit + self.profile.credit_recovery_rate
        )


# ========== 盲区画像管理器 ==========

class BlindSpotProfileManager:
    """盲区画像管理器"""

    def __init__(self, store: SignalStore = None):
        self.store = store or get_signal_store()
        self.detector = BlindSpotDetector(store)
        self.balancer = ChallengeBalancer()

    def analyze_and_update(self, session_context: Dict, user_options: List[Dict],
                           persona: PreferenceProfile) -> List[BlindSpot]:
        """
        分析盲区并更新画像。

        Returns:
            建议挑战的盲区列表（已过滤）
        """
        # 1. 加载当前盲区画像
        current_profile = self._load_profile()
        self.balancer.profile = current_profile

        # 2. 检测盲区
        blindspots = self.detector.detect(session_context, user_options, persona, current_profile)

        # 3. 挑战平衡
        should_challenge, to_challenge, reason = self.balancer.should_challenge(session_context, blindspots)

        # 4. 更新suspected列表
        for bs in blindspots:
            if bs.type not in [b.type for b in current_profile.confirmed + current_profile.suspected]:
                current_profile.suspected.append(bs)

        # 5. 保存
        self._save_profile(current_profile)

        if should_challenge:
            return to_challenge
        return []

    def record_challenge_outcome(self, blindspot_type: str, reaction: str,
                                  session_id: str = "", challenge_message: str = ""):
        """记录挑战结果"""
        profile = self._load_profile()
        self.balancer.profile = profile

        # 记录反应
        self.balancer.record_reaction(
            challenge_id=f"{session_id}_{blindspot_type}",
            reaction=reaction,
        )

        # 更新盲区状态
        for bs in profile.suspected:
            if bs.type == blindspot_type:
                bs.challenge_count += 1
                bs.last_challenged = datetime.now().isoformat()
                bs.user_reaction = reaction

                if reaction == "accepted":
                    # 移到confirmed
                    bs.status = "confirmed"
                    profile.confirmed.append(bs)
                    profile.suspected = [s for s in profile.suspected if s.type != blindspot_type]
                elif reaction == "rejected" and bs.challenge_count >= 3:
                    # 多次拒绝，移到dismissed
                    bs.status = "dismissed"
                    profile.dismissed.append(bs)
                    profile.suspected = [s for s in profile.suspected if s.type != blindspot_type]

                break

        self._save_profile(profile)

    def _load_profile(self) -> BlindSpotProfile:
        """从数据库加载盲区画像"""
        # 简化版：从persona_versions表加载
        try:
            latest = self.store.get_latest_persona_version()
            if latest and latest.get("blindspot_profile"):
                data = latest["blindspot_profile"]
                return self._dict_to_profile(data)
        except Exception as e:
            logger.warning(f"忽略异常: {e}")
        return BlindSpotProfile()

    def _save_profile(self, profile: BlindSpotProfile):
        """保存盲区画像到数据库（附加到最新persona版本）。"""
        from dataclasses import asdict
        data = {
            "confirmed": [asdict(b) for b in profile.confirmed],
            "suspected": [asdict(b) for b in profile.suspected],
            "dismissed": [asdict(b) for b in profile.dismissed],
            "total_challenges": profile.total_challenges,
            "accepted_count": profile.accepted_count,
            "ignored_count": profile.ignored_count,
            "rejected_count": profile.rejected_count,
            "acceptance_rate": profile.acceptance_rate,
            "challenge_credit": profile.challenge_credit,
        }
        self.store.update_blindspot_profile(data)

    def _dict_to_profile(self, data: Dict) -> BlindSpotProfile:
        """字典转盲区画像"""
        profile = BlindSpotProfile()
        profile.confirmed = [BlindSpot(**b) for b in data.get("confirmed", [])]
        profile.suspected = [BlindSpot(**b) for b in data.get("suspected", [])]
        profile.dismissed = [BlindSpot(**b) for b in data.get("dismissed", [])]
        profile.total_challenges = data.get("total_challenges", 0)
        profile.accepted_count = data.get("accepted_count", 0)
        profile.ignored_count = data.get("ignored_count", 0)
        profile.rejected_count = data.get("rejected_count", 0)
        profile.acceptance_rate = data.get("acceptance_rate", 0.0)
        profile.challenge_credit = data.get("challenge_credit", 10.0)
        return profile


# ========== 便捷函数 ==========

def detect_blindspots(session_context: Dict, user_options: List[Dict],
                      persona: PreferenceProfile) -> List[BlindSpot]:
    """便捷函数：检测盲区"""
    manager = BlindSpotProfileManager()
    return manager.analyze_and_update(session_context, user_options, persona)


def should_challenge_user(session_context: Dict, blindspots: List[BlindSpot]) -> Tuple[bool, List[BlindSpot], str]:
    """便捷函数：判断是否应该挑战"""
    balancer = ChallengeBalancer()
    return balancer.should_challenge(session_context, blindspots)


# 兼容别名
BlindspotAnalyzer = BlindSpotDetector

if __name__ == "__main__":
    # 测试
    detector = BlindSpotDetector()
    print("✅ BlindSpotDetector initialized")
