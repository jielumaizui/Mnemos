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


import hashlib
import json
from typing import Any, Dict, List, Mapping, Optional, Tuple
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from collections import Counter

from core.cognitive.user_model_asset_store import (
    UserCognitiveBlindspotStore,
    UserModelAssetStoreError,
)
from core.cognitive.user_model_assets import (
    AssetScope,
    CognitiveAuthorityEvidence,
    UserCognitiveBlindspot,
)
from core.config import get_config
from core.evidence.source_authority import SourceAuthorityCatalog

from .psyche import SignalStore, get_signal_store
from .pythia import PreferenceProfile
import logging

# Constants extracted from magic numbers
BLIND_SPOT_DETECTOR_DURATION_BUCKET_QUARTER_DAYS = 90
BLIND_SPOT_DETECTOR_DURATION_BUCKET_MONTH_DAYS = 30

logger = logging.getLogger(__name__)


# ========== 数据类 ==========


@dataclass
class BlindspotHypothesis:
    """Ephemeral assistant inference; never an active Persona asset."""

    type: str
    description: str
    evidence: List[str]
    confidence: float = 0.0
    first_detected: str = ""


BlindSpot = BlindspotHypothesis


@dataclass(frozen=True)
class CanonicalBlindspotChallenge:
    """One presentable challenge bound to an admitted current asset revision."""

    type: str
    description: str
    evidence: tuple[str, ...]
    confidence: float
    asset_id: str
    asset_revision_id: str
    asset_revision_hash: str
    challenge_content: str
    challenge_content_hash: str
    status: str
    expires_at: str
    source_kind: str = "canonical_admitted_blindspot"


@dataclass(frozen=True)
class BlindspotAdmission:
    """Explicit high-authority request to admit one detected hypothesis."""

    blindspot_type: str
    user_goal_ref: str
    impact: str
    scope: AssetScope
    expires_at: str
    invalidation_condition: str
    evidence: tuple[CognitiveAuthorityEvidence, ...]

    def __post_init__(self) -> None:
        if not all(
            str(value).strip()
            for value in (
                self.blindspot_type,
                self.user_goal_ref,
                self.impact,
                self.expires_at,
                self.invalidation_condition,
            )
        ) or not self.evidence:
            raise ValueError("blindspot admission requires goal, impact, scope, and evidence")


def _canonical_command_hash(payload: Mapping[str, Any]) -> str:
    encoded = json.dumps(
        dict(payload), ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


def _require_sha256(value: str, *, field_name: str) -> str:
    normalized = str(value or "").strip()
    digest = normalized.split(":", 1)[1] if normalized.startswith("sha256:") else ""
    if len(digest) != 64 or any(char not in "0123456789abcdef" for char in digest):
        raise ValueError(f"{field_name} must be an exact SHA-256")
    return normalized


@dataclass(frozen=True)
class BlindspotDecisionContext:
    """Exact decision lineage required before a shadow blindspot becomes an asset."""

    decision_id: str
    decision_trace_revision_id: str
    decision_trace_hash: str
    session_id: str
    project_id: str
    persona_revision_id: str

    def __post_init__(self) -> None:
        if not all(
            str(value or "").strip()
            for value in (
                self.decision_id,
                self.decision_trace_revision_id,
                self.session_id,
                self.project_id,
                self.persona_revision_id,
            )
        ):
            raise ValueError("blindspot admission requires an exact decision context")
        _require_sha256(self.decision_trace_hash, field_name="decision_trace_hash")

    def as_dict(self) -> dict[str, str]:
        return {
            "decision_id": self.decision_id,
            "decision_trace_revision_id": self.decision_trace_revision_id,
            "decision_trace_hash": self.decision_trace_hash,
            "session_id": self.session_id,
            "project_id": self.project_id,
            "persona_revision_id": self.persona_revision_id,
        }


@dataclass(frozen=True)
class BlindspotAdmissionCommand:
    """One high-authority, replay-safe request to admit a shadow hypothesis."""

    command_id: str
    idempotency_key: str
    decision_context: BlindspotDecisionContext
    admission: BlindspotAdmission
    source_authority_catalog_hash: str

    def __post_init__(self) -> None:
        if not self.command_id.strip() or not self.idempotency_key.strip():
            raise ValueError("blindspot admission command requires command and idempotency IDs")
        _require_sha256(
            self.source_authority_catalog_hash,
            field_name="source_authority_catalog_hash",
        )
        if not self.admission.scope.principal_id.strip():
            raise ValueError("blindspot admission scope requires an authenticated principal")

    @property
    def command_hash(self) -> str:
        return _canonical_command_hash(
            {
                "command_id": self.command_id,
                "idempotency_key": self.idempotency_key,
                "decision_context": self.decision_context.as_dict(),
                "admission": {
                    "blindspot_type": self.admission.blindspot_type,
                    "user_goal_ref": self.admission.user_goal_ref,
                    "impact": self.admission.impact,
                    "scope_key": self.admission.scope.key,
                    "expires_at": self.admission.expires_at,
                    "invalidation_condition": self.admission.invalidation_condition,
                    "evidence_refs": [item.evidence_ref for item in self.admission.evidence],
                },
                "source_authority_catalog_hash": self.source_authority_catalog_hash,
            }
        )

    def validate_catalog(self, catalog: SourceAuthorityCatalog) -> None:
        if not isinstance(catalog, SourceAuthorityCatalog):
            raise TypeError("blindspot admission requires a typed SourceAuthorityCatalog")
        if catalog.catalog_hash != self.source_authority_catalog_hash:
            raise ValueError("blindspot admission catalog hash mismatch")
        for evidence in self.admission.evidence:
            evidence.verify(catalog)


@dataclass(frozen=True)
class BlindspotAdmissionReceipt:
    """Durable result for one canonical blindspot admission command."""

    status: str
    command_id: str
    asset_id: str = ""
    revision_id: str = ""


@dataclass
class ChallengeRecord:
    """挑战记录"""

    id: str
    timestamp: str
    session_id: str
    blindspot_type: str
    challenge_message: str
    user_reaction: str = ""  # accepted/ignored/rejected
    outcome: str = ""  # 用户实际行为变化
    challenge_credit_cost: float = 1.0  # 消耗的信用额度


@dataclass
class BlindSpotProfile:
    """盲区画像"""

    confirmed: List[UserCognitiveBlindspot] = field(default_factory=list)
    suspected: List[UserCognitiveBlindspot] = field(default_factory=list)
    dismissed: List[UserCognitiveBlindspot] = field(default_factory=list)

    # 挑战统计
    total_challenges: int = 0
    accepted_count: int = 0
    ignored_count: int = 0
    rejected_count: int = 0
    acceptance_rate: float = 0.0

    # 信用系统
    challenge_credit: float = 10.0  # 当前信用额度
    credit_max: float = 10.0  # 最大额度
    credit_recovery_rate: float = 1.0  # 每天恢复速度

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
                "options_all_share_same_premise",  # 所有选项共享同一前提
                "no_framework_questioning",  # 用户从未质疑问题本身
                "historical_framing_shifts",  # 历史上用户曾被点出框架问题
            ],
        },
        "option_gap": {
            "name": "选项盲区",
            "description": "用户未意识到存在完全未考虑的替代方案",
            "signals": [
                "only_two_options_presented",  # 只呈现了两个选项
                "historical_third_options",  # 历史上同类决策有第三选项
                "no_exploration_behavior",  # 用户没有探索行为
            ],
        },
        "temporal": {
            "name": "时间盲区",
            "description": "用户过度关注短期效果，忽略长期影响和累积效应",
            "signals": [
                "all_options_short_term",  # 所有选项都是短期方案
                "no_maintenance_consideration",  # 不考虑维护成本
                "historical_long_term_issues",  # 历史上有长期问题未考虑
            ],
        },
        "preference_rigidity": {
            "name": "偏好僵化",
            "description": "用户被近期习惯绑架，没有考虑情境变化",
            "signals": [
                "same_choice_pattern",  # 连续多次做相同选择
                "context_changed_but_choice_didnt",  # 情境变了但选择没变
                "no_deviation_from_baseline",  # 从未偏离基线偏好
            ],
        },
    }

    def __init__(self, store: SignalStore | None = None):
        self.store = store or get_signal_store()

    def detect(
        self,
        session_context: Dict,
        user_options: List[Dict],
        persona: PreferenceProfile,
        history: BlindSpotProfile,
    ) -> List[BlindSpot]:
        """
        检测当前决策场景中的盲区。

        Args:
            session_context: 当前session上下文
            user_options: 用户正在考虑的选项列表（可为空，自动从上下文提取）
            persona: 当前用户画像
            history: 历史盲区画像

        Returns:
            检测到的盲区列表（按置信度排序）
        """
        # 自动从上下文提取选项（无 options 模式）
        if not user_options:
            user_options = self._extract_options_from_context(session_context)

        blindspots = []
        enabled_rules = self.DETECTION_RULES

        # 1. 框架盲区检测（需至少 2 个选项）
        if "framing" in enabled_rules and len(user_options) >= 2:
            framing = self._detect_framing_blindspot(session_context, user_options, history)
            if framing:
                blindspots.append(framing)

        # 2. 选项盲区检测
        if "option_gap" in enabled_rules:
            option_gap = self._detect_option_gap(session_context, user_options, persona, history)
            if option_gap:
                blindspots.append(option_gap)

        # 3. 时间盲区检测（需至少 1 个选项）
        if "temporal" in enabled_rules and user_options:
            temporal = self._detect_temporal_blindspot(session_context, user_options, history)
            if temporal:
                blindspots.append(temporal)

        # 4. 偏好僵化检测
        if "preference_rigidity" in enabled_rules:
            rigidity = self._detect_preference_rigidity(
                session_context, user_options, persona, history
            )
            if rigidity:
                blindspots.append(rigidity)

        # These remain ephemeral assistant inferences.  Only the manager may
        # admit one into the active Persona store after canonical source-
        # authority verification.
        blindspots.sort(key=lambda x: x.confidence, reverse=True)
        return blindspots

    def _detect_framing_blindspot(
        self, session_context, user_options, history
    ) -> Optional[BlindSpot]:
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
        historical_framing = [
            b for b in history.confirmed + history.suspected if b.type == "framing"
        ]

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
            description=f"你的{len(user_options)}个选项都基于同一个前提「{list(unique_premises)[0]}」，但没有考虑是否可以跳出这个前提",  # noqa: E501
            evidence=[
                f"所有选项共享前提: {list(unique_premises)[0]}",
                f"选项相似度: {similarity:.2f}",
            ]
            + (["历史上曾被点出框架盲区"] if historical_framing else []),
            confidence=min(1.0, confidence),
            first_detected=datetime.now().isoformat(),
        )

    def _detect_option_gap(
        self, session_context, user_options, persona, history
    ) -> Optional[BlindSpot]:
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
            evidence = (
                f"同类决策通常有{typical_option_count}个维度，你只覆盖了{len(user_options)}个"
            )

        if confidence < 0.6:
            return None

        return BlindSpot(
            type="option_gap",
            description=f"你目前只考虑了{len(user_options)}个选项，但这类决策通常还有你没覆盖的维度",
            evidence=[evidence],
            confidence=min(1.0, confidence),
            first_detected=datetime.now().isoformat(),
        )

    def _detect_temporal_blindspot(
        self, session_context, user_options, history
    ) -> Optional[BlindSpot]:
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
        historical_temporal = [
            b for b in history.confirmed + history.suspected if b.type == "temporal"
        ]

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
            ]
            + (["历史上曾被点出时间盲区"] if historical_temporal else []),
            confidence=min(1.0, confidence),
            first_detected=datetime.now().isoformat(),
        )

    def _detect_preference_rigidity(
        self, session_context, user_options, persona, history
    ) -> Optional[BlindSpot]:
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
            ]
            + (["当前情境与历史不同"] if context_changed else []),
            confidence=min(1.0, confidence),
            first_detected=datetime.now().isoformat(),
        )

    # ---- 辅助方法 ----

    def _extract_options_from_context(self, session_context: Dict) -> List[Dict]:
        """从 session 上下文中提取候选选项（无 options 模式）。

        启发式规则：
        - 若上下文包含 "options"/"choices"/"方案" 列表，直接解析
        - 否则从 user_message 中提取 "A 还是 B"、"对比" 等决策关键词
        """
        # 1. 尝试提取显式选项列表
        for key in ("options", "choices", "candidates", "方案"):
            if key in session_context and isinstance(session_context[key], list):
                return [
                    {"premise": str(opt), "keywords": [str(opt)]} for opt in session_context[key]
                ]

        # 2. 从用户消息中提取决策模式
        msg = session_context.get("user_message", "")
        if not msg:
            return []

        # 简单启发：消息中含 "还是"、"或者"、"对比"、"vs" 等词时提取两侧内容
        import re

        decision_patterns = [
            r"(.+?)(?:还是|或者|vs|versus|对比|相比)(.+)",
        ]
        for pattern in decision_patterns:
            m = re.search(pattern, msg, re.IGNORECASE)
            if m:
                return [
                    {"premise": m.group(1).strip(), "keywords": [m.group(1).strip()]},
                    {"premise": m.group(2).strip(), "keywords": [m.group(2).strip()]},
                ]

        # 3. 无明确决策信号时返回单选项（让 option_gap 有机会触发）
        return [{"premise": msg[:50], "keywords": [msg[:50]]}]

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
        try:
            rows = self.store.get_recent_session_signals(
                days=BLIND_SPOT_DETECTOR_DURATION_BUCKET_QUARTER_DAYS
            )
            task_type = session_context.get("task_type", "").lower()
            counts = []
            for r in rows:
                opts = r.get("options_presented", 0)
                if opts <= 0:
                    continue
                # 如果当前上下文有任务类型，优先匹配同类任务
                if task_type:
                    row_task = r.get("task_type", "").lower()
                    if task_type in row_task or row_task in task_type:
                        counts.append(int(opts))
                else:
                    counts.append(int(opts))
            return counts[:50]
        except (OSError, ValueError):
            logger.debug("获取历史选项模式失败", exc_info=True)
            return []

    def _get_recent_selections(self, session_context) -> List[Dict]:
        """获取最近的选择记录（从 session_signals 查询近期有选项的会话）"""
        try:
            rows = self.store.get_recent_session_signals(
                days=BLIND_SPOT_DETECTOR_DURATION_BUCKET_MONTH_DAYS
            )
            selections = []
            for r in rows:
                if r.get("options_presented", 0) > 0:
                    selections.append(
                        {
                            "option_type": r.get("task_type", ""),
                            "context_hash": r.get("working_dir", ""),
                            "timestamp": r.get("timestamp", ""),
                        }
                    )
            return selections[:10]  # 取最近 10 条
        except (OSError, ValueError):
            logger.debug("获取近期选择记录失败", exc_info=True)
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

    def __init__(self, profile: BlindSpotProfile | None = None):
        self.profile = profile or BlindSpotProfile()
        self.last_reaction_context: Dict[str, str] = {}

    def should_challenge(
        self, session_context: Dict, blindspots: List[BlindSpot]
    ) -> Tuple[bool, List[BlindSpot], str]:
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

    def record_reaction(self, _challenge_id: str, reaction: str, outcome: str = ""):
        """记录用户对挑战的反应"""
        self.last_reaction_context = {
            "challenge_id": _challenge_id,
            "reaction": reaction,
            "outcome": outcome,
        }
        self.profile.total_challenges += 1

        if reaction == "accepted":
            self.profile.accepted_count += 1
            self.profile.challenge_credit = min(
                self.profile.credit_max, self.profile.challenge_credit + 2.0  # 接受挑战，信用+2
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
            self.profile.challenge_credit + self.profile.credit_recovery_rate,
        )


# ========== 盲区画像管理器 ==========


class BlindSpotProfileManager:
    """盲区画像管理器"""

    def __init__(
        self,
        store: SignalStore | None = None,
        *,
        asset_store: UserCognitiveBlindspotStore | None = None,
    ):
        self.store = store or get_signal_store()
        self.detector = BlindSpotDetector(store)
        self.asset_store = asset_store or UserCognitiveBlindspotStore(
            get_config().database_dir / "user_cognitive_blindspots.db"
        )
        self.balancer = ChallengeBalancer()
        self.last_challenge_record: ChallengeRecord | None = None
        self.last_admission_receipts: tuple[BlindspotAdmissionReceipt, ...] = ()
        self.last_challenge_disposition = ""

    def analyze_and_update(
        self,
        session_context: Dict,
        user_options: List[Dict],
        persona: PreferenceProfile,
        *,
        admission_commands: tuple[BlindspotAdmissionCommand, ...] = (),
        source_authority_catalog: SourceAuthorityCatalog | None = None,
    ) -> List[BlindSpot]:
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

        # 3. Detector output is a shadow hypothesis.  Admit only an exact
        # requested type backed by the immutable high-authority catalog.
        catalog = source_authority_catalog
        if admission_commands and catalog is None:
            raise ValueError("blindspot admissions require SourceAuthorityCatalog")
        hypothesis_by_type = {item.type: item for item in blindspots}
        receipts: list[BlindspotAdmissionReceipt] = []
        for command in admission_commands:
            assert catalog is not None
            command.validate_catalog(catalog)
            self._validate_admission_context(command, session_context)
            admission = command.admission
            hypothesis = hypothesis_by_type.get(admission.blindspot_type)
            if hypothesis is None:
                receipts.append(
                    BlindspotAdmissionReceipt(
                        status="not_admitted",
                        command_id=command.command_id,
                    )
                )
                continue
            authority_refs = tuple(item.evidence_ref for item in admission.evidence)
            active = UserCognitiveBlindspot.create(
                blindspot_type=hypothesis.type,
                description=hypothesis.description,
                evidence_refs=(
                    *(f"assistant-hypothesis:{value}" for value in hypothesis.evidence),
                    *authority_refs,
                ),
                user_goal_ref=admission.user_goal_ref,
                impact=admission.impact,
                scope=admission.scope,
                confidence=hypothesis.confidence,
                expires_at=admission.expires_at,
                invalidation_condition=admission.invalidation_condition,
                first_detected=hypothesis.first_detected,
                authority_evidence_refs=authority_refs,
                admission_command_id=command.command_id,
                admission_command_hash=command.command_hash,
                admission_idempotency_key=command.idempotency_key,
                decision_context=command.decision_context.as_dict(),
            )
            existing = self.asset_store.current_blindspot(active.asset_id)
            if existing is not None:
                if (
                    existing.admission_idempotency_key != command.idempotency_key
                    or existing.admission_command_hash != command.command_hash
                ):
                    raise UserModelAssetStoreError(
                        "blindspot admission idempotency key is already bound to different semantics"
                    )
                receipts.append(
                    BlindspotAdmissionReceipt(
                        status="replayed",
                        command_id=command.command_id,
                        asset_id=existing.asset_id,
                        revision_id=existing.revision_id,
                    )
                )
                continue
            inserted = self.asset_store.persist(
                active,
                evidence=admission.evidence,
                catalog=catalog,
            )
            if not inserted:
                replay = self.asset_store.current_blindspot(active.asset_id)
                if (
                    replay is not None
                    and replay.admission_idempotency_key == command.idempotency_key
                    and replay.admission_command_hash == command.command_hash
                ):
                    receipts.append(
                        BlindspotAdmissionReceipt(
                            status="replayed",
                            command_id=command.command_id,
                            asset_id=replay.asset_id,
                            revision_id=replay.revision_id,
                        )
                    )
                    continue
                raise UserModelAssetStoreError(
                    "blindspot admission idempotency key is already bound to different semantics"
                )
            receipts.append(
                BlindspotAdmissionReceipt(
                    status="committed",
                    command_id=command.command_id,
                    asset_id=active.asset_id,
                    revision_id=active.revision_id,
                )
            )
        self.last_admission_receipts = tuple(receipts)

        # Canonical assets, not the detector output, are projected to Persona.
        self._replace_assets_from_canonical_store(current_profile)

        # 4. A user-facing challenge is derived only from an admitted current
        # canonical revision that still matches principal, scope, status, and
        # expiry. Detector output remains internal shadow material.
        canonical_challenges = [
            challenge
            for asset in (*current_profile.suspected, *current_profile.confirmed)
            if (
                challenge := self._canonical_challenge_for_context(
                    asset,
                    session_context,
                )
            )
            is not None
        ]
        should_challenge, to_challenge, reason = self.balancer.should_challenge(
            session_context,
            canonical_challenges,
        )
        self.last_challenge_disposition = (
            reason if canonical_challenges else "no_admitted_canonical_revision"
        )

        # 5. Only a newly committed admission may update the legacy Persona
        # projection. Evaluation/noop/replay must not create a business write;
        # presentation and reaction telemetry are recorded by their own owners.
        if any(receipt.status == "committed" for receipt in receipts):
            self._save_profile(current_profile)

        if should_challenge:
            return to_challenge
        return []

    @staticmethod
    def _canonical_challenge_for_context(
        asset: UserCognitiveBlindspot,
        session_context: Mapping[str, Any],
    ) -> CanonicalBlindspotChallenge | None:
        """Fail closed unless one current canonical revision is presentable now."""

        principal_id = str(session_context.get("principal_id") or "").strip()
        if not principal_id or asset.principal_id != principal_id:
            return None
        if asset.status not in {"suspected", "confirmed"}:
            return None
        if asset.purpose != "decision_support":
            return None
        scope_values = {
            "session": str(session_context.get("session_id") or "").strip(),
            "project": str(session_context.get("project_id") or "").strip(),
            "user": principal_id,
            "global": "global",
        }
        if asset.scope_type not in scope_values or asset.scope_id != scope_values[asset.scope_type]:
            return None
        try:
            expires_at = datetime.fromisoformat(asset.expires_at.replace("Z", "+00:00"))
            evaluated_at = datetime.fromisoformat(
                str(
                    session_context.get("decision_created_at")
                    or datetime.now(timezone.utc).isoformat()
                ).replace("Z", "+00:00")
            )
            if expires_at.tzinfo is None:
                expires_at = expires_at.replace(tzinfo=timezone.utc)
            if evaluated_at.tzinfo is None:
                evaluated_at = evaluated_at.replace(tzinfo=timezone.utc)
        except (TypeError, ValueError):
            return None
        if expires_at <= evaluated_at:
            return None
        if (
            not asset.asset_id
            or not asset.revision_id
            or not asset.admission_command_id
            or not asset.authority_evidence_refs
        ):
            return None
        asset_hash = _canonical_command_hash(asdict(asset))
        content = str(asset.description or "").strip()
        if not content:
            return None
        content_hash = _canonical_command_hash(
            {
                "asset_id": asset.asset_id,
                "asset_revision_id": asset.revision_id,
                "type": asset.type,
                "content": content,
                "impact": asset.impact,
            }
        )
        return CanonicalBlindspotChallenge(
            type=asset.type,
            description=content,
            evidence=tuple(asset.evidence),
            confidence=asset.confidence,
            asset_id=asset.asset_id,
            asset_revision_id=asset.revision_id,
            asset_revision_hash=asset_hash,
            challenge_content=content,
            challenge_content_hash=content_hash,
            status=asset.status,
            expires_at=asset.expires_at,
        )

    @staticmethod
    def _validate_admission_context(
        command: BlindspotAdmissionCommand,
        session_context: Mapping[str, Any],
    ) -> None:
        context = command.decision_context
        for field_name, actual in (
            ("session_id", session_context.get("session_id", "")),
            ("project_id", session_context.get("project_id", "")),
            ("decision_id", session_context.get("decision_id", "")),
        ):
            if str(actual or "").strip() != getattr(context, field_name):
                raise ValueError(f"blindspot admission {field_name} does not match runtime context")

    def record_challenge_outcome(
        self,
        blindspot_type: str,
        reaction: str,
        session_id: str = "",
        _challenge_message: str = "",
        *,
        asset_id: str = "",
        outcome: str = "",
        outcome_evidence: tuple[CognitiveAuthorityEvidence, ...] = (),
        source_authority_catalog: SourceAuthorityCatalog | None = None,
    ):
        """Record challenge reaction separately from blindspot validation."""
        profile = self._load_profile()
        self.balancer.profile = profile
        challenge_id = f"{session_id}_{asset_id or blindspot_type}"
        challenge_timestamp = datetime.now().isoformat()
        self.last_challenge_record = ChallengeRecord(
            id=challenge_id,
            timestamp=challenge_timestamp,
            session_id=session_id,
            blindspot_type=blindspot_type,
            challenge_message=_challenge_message,
            user_reaction=reaction,
            outcome=outcome,
        )

        # 记录反应
        self.balancer.record_reaction(
            _challenge_id=challenge_id,
            reaction=reaction,
        )

        # A reaction updates challenge telemetry only.  It never confirms a
        # cognitive defect.  State changes require exact, high-authority
        # evidence bound to the current canonical revision.
        candidates = [
            bs
            for bs in profile.suspected
            if bs.type == blindspot_type and (not asset_id or bs.asset_id == asset_id)
        ]
        target_bs = candidates[0] if len(candidates) == 1 else None

        if target_bs:
            if outcome in {"validated", "invalidated"} and outcome_evidence:
                if source_authority_catalog is None:
                    raise ValueError("blindspot outcome requires SourceAuthorityCatalog")
                next_status = "confirmed" if outcome == "validated" else "dismissed"
                self.asset_store.transition_blindspot(
                    target_bs.asset_id,
                    expected_revision_id=target_bs.revision_id,
                    next_status=next_status,
                    evidence=outcome_evidence,
                    catalog=source_authority_catalog,
                    payload_updates={
                        "challenge_count": target_bs.challenge_count + 1,
                        "last_challenged": challenge_timestamp,
                        "user_reaction": reaction,
                    },
                )

        self._replace_assets_from_canonical_store(profile)

        self._save_profile(profile)

    def recover_credit(self) -> float:
        """恢复挑战信用并持久化，供周期性画像分析/每日任务调用。"""
        profile = self._load_profile()
        self.balancer.profile = profile
        self.balancer.recover_credit()
        self._save_profile(self.balancer.profile)
        return self.balancer.profile.challenge_credit

    def _load_profile(self) -> BlindSpotProfile:
        """Load challenge telemetry and overlay canonical cognitive assets."""
        profile = BlindSpotProfile()
        try:
            latest = self.store.get_latest_persona_version()
            if latest and latest.get("blindspot_profile"):
                data = latest["blindspot_profile"]
                profile = self._dict_to_profile(data)
        except (OSError, ValueError) as e:
            logger.warning("加载盲区画像失败: %s", e, exc_info=True)
        self._replace_assets_from_canonical_store(profile)
        return profile

    def _replace_assets_from_canonical_store(self, profile: BlindSpotProfile) -> None:
        """Project independent typed revisions into the legacy profile view."""

        state = self.asset_store.schema_status()
        if state["status"] == "uninitialized":
            profile.confirmed = []
            profile.suspected = []
            profile.dismissed = []
            return
        if not state["ok"]:
            raise UserModelAssetStoreError(
                "user cognitive blindspot store requires explicit reconciliation"
            )
        assets = self.asset_store.current_blindspots()
        profile.confirmed = [item for item in assets if item.status == "confirmed"]
        profile.suspected = [item for item in assets if item.status == "suspected"]
        profile.dismissed = [item for item in assets if item.status == "dismissed"]

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
            "credit_max": profile.credit_max,
            "credit_recovery_rate": profile.credit_recovery_rate,
        }
        evidence_refs = tuple(
            dict.fromkeys(
                (
                    *(
                        f"blindspot-evidence:{evidence}"
                        for blindspot in (
                            profile.confirmed
                            + profile.suspected
                            + profile.dismissed
                        )
                        for evidence in blindspot.evidence
                        if evidence
                    ),
                    *(
                        (f"challenge:{self.last_challenge_record.id}",)
                        if self.last_challenge_record is not None
                        else ()
                    ),
                    "persona-blindspot-profile:current",
                )
            )
        )
        created_at = datetime.now().astimezone().isoformat()
        material_action = self.store.prepare_blindspot_material_action(
            data,
            source_facts={
                "profile": data,
                "challenge": (
                    asdict(self.last_challenge_record)
                    if self.last_challenge_record is not None
                    else {}
                ),
            },
            evidence_refs=evidence_refs,
            created_at=created_at,
        )
        self.store.update_blindspot_profile(
            data,
            material_action=material_action,
        )

    def _dict_to_profile(self, data: Dict) -> BlindSpotProfile:
        """Load challenge telemetry; legacy blindspot objects are not active."""
        profile = BlindSpotProfile()
        profile.total_challenges = data.get("total_challenges", 0)
        profile.accepted_count = data.get("accepted_count", 0)
        profile.ignored_count = data.get("ignored_count", 0)
        profile.rejected_count = data.get("rejected_count", 0)
        profile.acceptance_rate = data.get("acceptance_rate", 0.0)
        profile.challenge_credit = data.get("challenge_credit", 10.0)
        profile.credit_max = data.get("credit_max", 10.0)
        profile.credit_recovery_rate = data.get("credit_recovery_rate", 1.0)
        return profile


class BlindspotAdmissionService:
    """The sole application entry point for canonical blindspot admissions."""

    def __init__(self, manager: BlindSpotProfileManager | None = None):
        self.manager = manager or BlindSpotProfileManager()

    def admit(
        self,
        command: BlindspotAdmissionCommand,
        *,
        session_context: Mapping[str, Any],
        user_options: List[Mapping[str, Any]],
        persona: PreferenceProfile,
        source_authority_catalog: SourceAuthorityCatalog,
    ) -> BlindspotAdmissionReceipt:
        self.manager.analyze_and_update(
            session_context=dict(session_context),
            user_options=[dict(option) for option in user_options],
            persona=persona,
            admission_commands=(command,),
            source_authority_catalog=source_authority_catalog,
        )
        if len(self.manager.last_admission_receipts) != 1:
            raise RuntimeError("blindspot admission command has no terminal receipt")
        return self.manager.last_admission_receipts[0]


# ========== 便捷函数 ==========


def detect_blindspots(
    session_context: Dict, user_options: List[Dict], persona: PreferenceProfile
) -> List[BlindSpot]:
    """便捷函数：检测盲区"""
    manager = BlindSpotProfileManager()
    return manager.analyze_and_update(session_context, user_options, persona)


def should_challenge_user(
    session_context: Dict, blindspots: List[BlindSpot]
) -> Tuple[bool, List[BlindSpot], str]:
    """便捷函数：判断是否应该挑战"""
    balancer = ChallengeBalancer()
    return balancer.should_challenge(session_context, blindspots)


# 兼容别名
BlindspotAnalyzer = BlindSpotDetector

if __name__ == "__main__":
    # 测试
    detector = BlindSpotDetector()
    print("✅ BlindSpotDetector initialized")
