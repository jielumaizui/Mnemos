"""
Deviation Detector — 偏差检测触发器

核心职责：
检测用户当前表达与历史 Observation 之间的偏差。
只在"用户认知与事实有偏差"时才触发 Mirror + Insight 注入。

触发流程（用户设计的精妙之处）：
    用户："我想策划一场活动"
        │
        ▼
    系统检测到触发关键词 → 进入「监听模式」
        │
        ▼
    用户继续输出想法...（系统静默累积上下文）
        │
        ├──→ 用户表达与 Observation 一致（无偏差）
        │         └──→ 继续静默，不打扰用户思考
        │
        └──→ 用户表达与 Observation 有偏差
                  └──→ 自动注入 Mirror + Insight

设计约束：
- 不依赖语义理解/LLM 推理做偏差判断
- 只做结构化数值比对（正则提取数字 + 单位）
- 非数值类偏差不检测（留给宿主 Agent 判断）
- 监听会话有超时机制（默认 10 分钟无输入则关闭）

偏差类型：
1. 数值偏差：用户声明的时间/数量与 Observation 数据不符
   例：用户说"预计2周完成"，但 Observation 显示平均延期 2.8 倍

2. 频率偏差：用户对某行为频率的自我评估与 Observation 不符
   例：用户说"我很少加班"，但 stress Observation 显示高频加班模式

3. 忽略偏差：用户在规划中未提及 Observation 中反复出现的风险因素
   例：用户策划活动时未考虑时间估算（但 time Observation 显示系统性低估）
"""

import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import Dict, List, Optional

from core.reflection.mirror_engine import MirrorResult


@dataclass
class DeviationSignal:
    """检测到的偏差信号"""

    deviation_type: str  # "numeric", "frequency", "omission"
    dimension: str
    user_claim: str  # 用户声称的内容（原文摘录）
    observed_fact: str  # Observation 显示的事实
    severity: float  # 偏差严重程度 0-1
    suggestion: str  # 建议注入的上下文摘要
    detected_at: datetime = field(default_factory=datetime.now)


@dataclass
class ListeningSession:
    """监听会话状态"""

    session_id: str
    trigger_scene: str  # 触发场景（如 "new_project", "major_decision"）
    user_messages: List[str] = field(default_factory=list)
    mirror: Optional[MirrorResult] = None
    started_at: datetime = field(default_factory=datetime.now)
    last_activity_at: datetime = field(default_factory=datetime.now)
    deviation_detected: bool = False
    deviation_signal: Optional[DeviationSignal] = None

    @property
    def is_expired(self, timeout_sec: float = 600.0) -> bool:
        """会话是否已超时（默认 10 分钟）"""
        return (datetime.now() - self.last_activity_at).total_seconds() > timeout_sec

    @property
    def duration_sec(self) -> float:
        """会话持续时间"""
        return (datetime.now() - self.started_at).total_seconds()


class DeviationDetector:
    """偏差检测器 — 代码层实现，不依赖语义理解"""

    # 数值提取正则：匹配 "数字 + 单位" 模式
    # 支持：2周、3个月、5天、100人、10万、1.5倍
    NUMERIC_PATTERN = re.compile(r"(\d+\.?\d*)\s*([个只条项场周月年天小时分钟次倍倍万人元块%％])")

    # 频率相关关键词（用于频率偏差检测）
    FREQUENCY_INDICATORS = {
        "经常": 0.8,
        "总是": 0.95,
        "常常": 0.75,
        "频繁": 0.8,
        "有时": 0.4,
        "偶尔": 0.2,
        "很少": 0.1,
        "从不": 0.0,
        "总是": 0.95,
        "一直": 0.9,
        "通常": 0.7,
    }

    # 偏差阈值
    NUMERIC_DEVIATION_THRESHOLD = 0.5  # 数值偏差超过 50% 视为显著
    FREQUENCY_DEVIATION_THRESHOLD = 0.4  # 频率评估偏差超过 0.4 视为显著
    OMISSION_THRESHOLD = 0.6  # 忽略风险因素的阈值

    def __init__(self):
        self._sessions: Dict[str, ListeningSession] = {}

    # ───────────────────────────────
    # 会话管理
    # ───────────────────────────────

    def start_listening(
        self,
        session_id: str,
        trigger_scene: str,
        mirror: MirrorResult,
    ) -> ListeningSession:
        """
        启动监听会话

        Args:
            session_id: 会话唯一标识（如用户 session_id）
            trigger_scene: 触发场景
            mirror: 预加载的 Mirror（相关 Observation 证据链）

        Returns:
            ListeningSession
        """
        session = ListeningSession(
            session_id=session_id,
            trigger_scene=trigger_scene,
            mirror=mirror,
        )
        self._sessions[session_id] = session
        return session

    def add_user_message(self, session_id: str, message: str) -> Optional[DeviationSignal]:
        """
        添加用户消息并检测偏差

        Args:
            session_id: 会话 ID
            message: 用户新输入的消息

        Returns:
            DeviationSignal if deviation detected, else None
        """
        session = self._sessions.get(session_id)
        if not session:
            return None

        if session.is_expired:
            self._sessions.pop(session_id, None)
            return None

        if session.deviation_detected:
            return session.deviation_signal

        # 累积消息
        session.user_messages.append(message)
        session.last_activity_at = datetime.now()

        # 执行偏差检测
        signal = self._detect_deviation(session, message)
        if signal:
            session.deviation_detected = True
            session.deviation_signal = signal
            return signal

        return None

    def get_session(self, session_id: str) -> Optional[ListeningSession]:
        """获取监听会话状态"""
        session = self._sessions.get(session_id)
        if session and session.is_expired:
            self._sessions.pop(session_id, None)
            return None
        return session

    def close_session(self, session_id: str):
        """关闭监听会话"""
        self._sessions.pop(session_id, None)

    # ───────────────────────────────
    # 偏差检测核心
    # ───────────────────────────────

    def _detect_deviation(
        self,
        session: ListeningSession,
        latest_message: str,
    ) -> Optional[DeviationSignal]:
        """
        检测偏差

        检测顺序：
        1. 数值偏差（结构化比对）
        2. 频率偏差（关键词匹配 + 数值比对）
        3. 忽略偏差（检查关键维度是否被用户提及）
        """
        if not session.mirror or not session.mirror.snapshots:
            return None

        # 1. 数值偏差检测
        numeric_signal = self._detect_numeric_deviation(session, latest_message)
        if numeric_signal:
            return numeric_signal

        # 2. 频率偏差检测
        freq_signal = self._detect_frequency_deviation(session, latest_message)
        if freq_signal:
            return freq_signal

        # 3. 忽略偏差检测（只在会话有一定长度后才检测）
        if len(session.user_messages) >= 3:
            omission_signal = self._detect_omission_deviation(session)
            if omission_signal:
                return omission_signal

        return None

    def _detect_numeric_deviation(
        self,
        session: ListeningSession,
        message: str,
    ) -> Optional[DeviationSignal]:
        """
        数值偏差检测

        策略：
        1. 从用户消息中提取数值声明（如"2周"、"5个人"）
        2. 查找 Mirror 中同维度的数值型 Observation
        3. 比对是否显著偏离
        """
        # 提取用户声明的数值
        user_numbers = self._extract_numbers(message)
        if not user_numbers:
            return None

        # 遍历 Mirror 中的数值证据
        for snap in session.mirror.snapshots:  # type: ignore[union-attr]
            obs_numbers = self._extract_numbers_from_observation(snap.value_summary)
            if not obs_numbers:
                continue

            # 比对数值（同单位或同数量级）
            for user_val, user_unit in user_numbers:
                for obs_val, obs_unit in obs_numbers:
                    # 单位匹配或可以转换
                    if not self._units_compatible(user_unit, obs_unit):
                        continue

                    # 计算偏差
                    if obs_val == 0:
                        continue

                    ratio = user_val / obs_val
                    deviation = abs(1.0 - ratio)

                    if deviation >= self.NUMERIC_DEVIATION_THRESHOLD:
                        severity = min(1.0, deviation)
                        return DeviationSignal(
                            deviation_type="numeric",
                            dimension=snap.dimension,
                            user_claim=f"{user_val}{user_unit}",
                            observed_fact=f"历史数据: {obs_val}{obs_unit}",
                            severity=round(severity, 2),
                            suggestion=(
                                f"你预计需要 {user_val}{user_unit}，"
                                f"但过去数据显示约为 {obs_val}{obs_unit}"
                            ),
                        )

        return None

    def _detect_frequency_deviation(
        self,
        session: ListeningSession,
        message: str,
    ) -> Optional[DeviationSignal]:
        """
        频率偏差检测

        策略：
        1. 检测用户消息中的频率自评关键词（"经常"、"很少"等）
        2. 与 Observation 中的实际频率数据比对
        """
        # 检测频率自评
        user_freq = None
        for keyword, score in self.FREQUENCY_INDICATORS.items():
            if keyword in message:
                user_freq = score
                break

        if user_freq is None:
            return None

        # 查找对应维度的频率数据
        for snap in session.mirror.snapshots:  # type: ignore[union-attr]
            if snap.dimension not in ["stress", "actions", "attention"]:
                continue

            # 从 value_summary 提取频率数值
            obs_freq = self._extract_frequency_from_summary(snap.value_summary)
            if obs_freq is None:
                continue

            # 比对
            freq_deviation = abs(user_freq - obs_freq)
            if freq_deviation >= self.FREQUENCY_DEVIATION_THRESHOLD:
                return DeviationSignal(
                    deviation_type="frequency",
                    dimension=snap.dimension,
                    user_claim=f"你认为自己{self._frequency_word(user_freq)}",
                    observed_fact=f"数据显示{self._frequency_word(obs_freq)}",
                    severity=round(min(1.0, freq_deviation), 2),
                    suggestion=(
                        f"你认为自己{self._frequency_word(user_freq)}，"
                        f"但数据显示实际{self._frequency_word(obs_freq)}"
                    ),
                )

        return None

    def _detect_omission_deviation(
        self,
        session: ListeningSession,
    ) -> Optional[DeviationSignal]:
        """
        忽略偏差检测

        策略：
        1. 检查 Mirror 中是否有高置信度、低时效权重的风险信号
        2. 检查用户累积输入中是否提及了这些风险
        3. 如果重要风险被持续忽略，触发偏差
        """
        # 获取用户所有输入的合并文本
        user_text = " ".join(session.user_messages).lower()

        # 检查每个 Mirror snapshot
        for snap in session.mirror.snapshots:  # type: ignore[union-attr]
            # 只检查高置信度但时效权重较低的（历史反复出现的模式）
            if snap.confidence < 0.7 or snap.recency_weight > 0.6:
                continue

            # 提取关键词
            keywords = self._extract_keywords(snap.value_summary)
            if not keywords:
                continue

            # 检查用户是否提到了这些关键词
            mentioned = sum(1 for kw in keywords if kw in user_text)
            coverage = mentioned / len(keywords) if keywords else 1.0
            omitted_ratio = 1.0 - coverage

            # 如果重要关键词被忽略，且该维度是历史风险维度
            if omitted_ratio >= self.OMISSION_THRESHOLD and snap.dimension in [
                "time",
                "stress",
                "decisions",
            ]:
                return DeviationSignal(
                    deviation_type="omission",
                    dimension=snap.dimension,
                    user_claim="规划中未考虑",
                    observed_fact=snap.value_summary[:80],
                    severity=round(snap.confidence * omitted_ratio, 2),
                    suggestion=(
                        f"你的规划中似乎未考虑 '{snap.dimension}' 维度的历史模式："
                        f"{snap.value_summary[:60]}"
                    ),
                )

        return None

    # ───────────────────────────────
    # 工具方法
    # ───────────────────────────────

    def _extract_numbers(self, text: str) -> List[tuple]:
        """从文本中提取数值+单位对"""
        matches = self.NUMERIC_PATTERN.findall(text)
        result = []
        for val_str, unit in matches:
            try:
                val = float(val_str)
                result.append((val, unit))
            except ValueError:
                continue
        return result

    def _extract_numbers_from_observation(self, value_summary: str) -> List[tuple]:
        """从 Observation value_summary 中提取数值"""
        return self._extract_numbers(value_summary)

    def _units_compatible(self, unit1: str, unit2: str) -> bool:
        """判断两个单位是否兼容（可比对）"""
        # 时间单位组
        time_units = {"周", "月", "年", "天", "小时", "分钟"}
        # 数量单位组
        count_units = {"个", "只", "条", "项", "场", "次", "人"}
        # 比例单位组
        ratio_units = {"倍", "%", "％"}

        if unit1 in time_units and unit2 in time_units:
            return True
        if unit1 in count_units and unit2 in count_units:
            return True
        if unit1 in ratio_units and unit2 in ratio_units:
            return True
        return unit1 == unit2

    def _extract_frequency_from_summary(self, summary: str) -> Optional[float]:
        """从 summary 中提取频率数值（0-1）"""
        # 尝试提取百分比
        pct_match = re.search(r"(\d+\.?\d*)\s*[%％]", summary)
        if pct_match:
            return float(pct_match.group(1)) / 100.0

        # 尝试提取频率描述
        summary_lower = summary.lower()
        for keyword, score in self.FREQUENCY_INDICATORS.items():
            if keyword in summary_lower:
                return score

        # 尝试提取比例（如 "3/10"）
        ratio_match = re.search(r"(\d+)\s*/\s*(\d+)", summary)
        if ratio_match:
            num = float(ratio_match.group(1))
            den = float(ratio_match.group(2))
            if den > 0:
                return num / den

        return None

    def _frequency_word(self, score: float) -> str:
        """将频率分数转换为中文描述"""
        if score >= 0.9:
            return "总是/几乎每次都"
        elif score >= 0.7:
            return "经常"
        elif score >= 0.5:
            return "有时"
        elif score >= 0.3:
            return "偶尔"
        elif score > 0:
            return "很少"
        return "从不"

    def _extract_keywords(self, text: str) -> List[str]:
        """提取文本中的关键词（简单分词）"""
        # 中文2字以上，英文3字母以上
        words = re.findall(r"[一-鿿]{2,}|[a-zA-Z_]{3,}", text.lower())
        return list(set(words))
