"""
AdaptiveScorerV2 — 自适应评分引擎 V2（空壳骨架）

【E14 骨架阶段】接口已定义，实现待数据积累后填充。
与 V1 的差异（ADR-016 修复清单）：
  1. _bayesian_update 使用显式 P(E|~H)，不再假设 = 1 - P(E|H)
  2. 训练标签来自 ground_truth_signals（外部真实信号），禁止自举
  3. 使用 partial_fit 增量更新，不覆盖已有模型
  4. EWMA 更新模型内部参数（feature_count_ / class_count_）
  5. _extract_features 返回 content 键（供 TfidfVectorizer 使用）

数据流：
  评分 → scorer_training_queue → 等待延迟信号 → ground_truth_signals
                                              ↑
  用户反馈/搜索命中/页面访问/盲区检测 ──────────┘
              ↓
  chronos 每小时 → process_training_queue → partial_fit
              ↓
  保存到 scorer_models

实施状态：
  - 接口定义：✅ 完成
  - 数据库表：✅ core/db_init.py 已创建
  - ground_truth 写入点：✅ core/hephaestus/deferred_distill.py 已接入
  - 算法实现：⏳ 等待 ground_truth 数据积累 ≥20 条后启动
"""

from __future__ import annotations

import json
import logging
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from core.config import get_config

logger = logging.getLogger(__name__)


# ==================== 数据模型 ====================

@dataclass(frozen=True)
class ScoreCardV2:
    """V2 评分卡"""
    scores: Dict[str, float]              # 各维度得分 [0.0, 1.0]
    confidences: Dict[str, float]         # 各维度置信度 [0.0, 1.0]
    features: Dict[str, Any]              # 提取的原始特征
    model_version: str                    # 模型版本
    timestamp: datetime = field(default_factory=datetime.now)


@dataclass(frozen=True)
class FeedbackV2:
    """V2 反馈信号"""
    session_id: str
    dimension: str
    expected: float                       # 正确值 [0.0, 1.0]
    actual: float                         # 系统给出的值 [0.0, 1.0]
    features: Dict[str, Any]              # 评分时的特征快照
    source: str = "manual"                # manual / api / auto
    timestamp: datetime = field(default_factory=datetime.now)


@dataclass(frozen=True)
class GroundTruth:
    """外部真实信号"""
    session_id: str
    signal_type: str                      # distill_complete / distill_skip / search_hit / page_view / user_feedback / blindspot
    label: int                            # 1=正例, 0=负例
    confidence: float = 1.0
    latency_hours: int = 0


# ==================== AdaptiveScorerV2（空壳） ====================

class AdaptiveScorerV2:
    """
    自适应评分引擎 V2（骨架阶段）。

    当前状态：接口已定义，核心算法待数据积累后实现。
    旁路部署中：评分走 V1 (core/scoring/adaptive_scorer.py)，
    V2 只负责接收 feedback 和积累 ground_truth 信号。
    """

    # 三阶段阈值
    COLD_THRESHOLD = 0
    WARM_THRESHOLD = 30
    HOT_THRESHOLD = 200

    def __init__(
        self,
        domain: str = "mnemos",
        config: Dict[str, Any] = None,
        db_path: Optional[str] = None,
    ):
        self.domain = domain
        self.config = config or {}
        self.db_path = Path(db_path) if db_path else (get_config().data_dir / "mnemos.db")
        self._mode = "cold"
        self._models: Dict[str, Any] = {}  # dimension → model
        self._model_versions: Dict[str, str] = {}

    # ── 核心评分接口（骨架：当前透传到 V1） ──

    def score(self, item: Any, dimensions: List[str]) -> ScoreCardV2:
        """
        多维度评分（骨架实现）。

        当前行为：透传到 V1 的 RuleScorer，返回空 ScoreCardV2。
        未来：特征提取 → 规则先验 → ML 似然 → 贝叶斯后验。
        """
        # TODO: 数据积累够后接入真实实现
        return ScoreCardV2(
            scores={dim: 0.5 for dim in dimensions},
            confidences={dim: 0.0 for dim in dimensions},
            features={},
            model_version="v2-skeleton",
        )

    def feedback(self, fb: FeedbackV2) -> None:
        """
        接收反馈，写入 ground_truth_signals 和 scorer_training_queue。

        这是当前唯一在运行的逻辑：积累训练数据。
        """
        self._insert_ground_truth(
            session_id=fb.session_id,
            signal_type="user_feedback",
            label=1 if fb.expected >= 0.5 else 0,
            confidence=abs(fb.expected - fb.actual),
        )
        self._insert_training_queue(fb)
        logger.debug(f"[ScorerV2] Feedback recorded for session={fb.session_id}")

    # ── 批量训练接口（骨架：当前为空操作） ──

    def process_training_queue(self, dimension: Optional[str] = None) -> int:
        """
        处理训练队列，返回本次训练的样本数。

        当前行为：统计 ready 记录数，但不训练（等待数据积累）。
        未来：消费延迟信号 → partial_fit → 保存模型。
        """
        ready_count = self._count_ready_samples(dimension)
        if ready_count < 20:
            logger.info(
                f"[ScorerV2] Training skipped: only {ready_count} ready samples "
                f"(need ≥20 to start first training)"
            )
            return 0

        logger.info(
            f"[ScorerV2] {ready_count} samples ready for training, "
            f"but algorithm implementation pending (skeleton stage)"
        )
        return ready_count

    # ── ground_truth 写入点（供各业务模块调用） ──

    @classmethod
    def insert_ground_truth(
        cls,
        session_id: str,
        signal_type: str,
        label: int,
        confidence: float = 1.0,
        latency_hours: int = 0,
        db_path: Optional[Path] = None,
    ) -> None:
        """
        静态方法：各业务模块直接调用，无需实例化 ScorerV2。

        Args:
            session_id: 关联的 session ID
            signal_type: distill_complete / distill_skip / search_hit / page_view / user_feedback / blindspot
            label: 1=正例, 0=负例
            confidence: 信号本身的置信度
            latency_hours: 信号延迟（小时），用于训练管道调度
            db_path: 数据库路径，None 则使用默认
        """
        db = db_path or (get_config().data_dir / "mnemos.db")
        try:
            with sqlite3.connect(str(db)) as conn:
                conn.execute("""
                    INSERT INTO ground_truth_signals
                        (session_id, signal_type, signal_value, confidence, latency_hours, created_at)
                    VALUES (?, ?, ?, ?, ?, ?)
                    ON CONFLICT(session_id, signal_type) DO UPDATE SET
                        signal_value = excluded.signal_value,
                        confidence = excluded.confidence,
                        created_at = excluded.created_at
                """, (
                    session_id, signal_type, label, confidence, latency_hours,
                    datetime.now().isoformat(),
                ))
                conn.commit()
        except Exception as e:
            logger.warning(f"[ScorerV2] ground_truth insert failed: {e}")

    # ── 内部方法 ──

    def _insert_ground_truth(self, session_id: str, signal_type: str,
                             label: int, confidence: float = 1.0) -> None:
        """实例方法封装静态方法"""
        self.insert_ground_truth(
            session_id=session_id,
            signal_type=signal_type,
            label=label,
            confidence=confidence,
            db_path=self.db_path,
        )

    def _insert_training_queue(self, fb: FeedbackV2) -> None:
        """将反馈写入训练队列"""
        try:
            with sqlite3.connect(str(self.db_path)) as conn:
                conn.execute("""
                    INSERT INTO scorer_training_queue
                        (session_id, dimension, features_json, priority, earliest_train_at, status)
                    VALUES (?, ?, ?, ?, ?, 'pending')
                """, (
                    fb.session_id,
                    fb.dimension,
                    json.dumps(fb.features, ensure_ascii=False, default=str),
                    10,  # user_feedback 优先级最高
                    (datetime.now() + timedelta(hours=0)).isoformat(),
                ))
                conn.commit()
        except Exception as e:
            logger.warning(f"[ScorerV2] training_queue insert failed: {e}")

    def _count_ready_samples(self, dimension: Optional[str] = None) -> int:
        """统计已到训练时间的样本数"""
        try:
            with sqlite3.connect(str(self.db_path)) as conn:
                now = datetime.now().isoformat()
                if dimension:
                    row = conn.execute("""
                        SELECT COUNT(*) FROM scorer_training_queue
                        WHERE status = 'pending'
                          AND earliest_train_at <= ?
                          AND dimension = ?
                    """, (now, dimension)).fetchone()
                else:
                    row = conn.execute("""
                        SELECT COUNT(*) FROM scorer_training_queue
                        WHERE status = 'pending'
                          AND earliest_train_at <= ?
                    """, (now,)).fetchone()
                return row[0] if row else 0
        except Exception:
            return 0

    def get_status(self) -> Dict[str, Any]:
        """获取 scorer 状态摘要"""
        return {
            "domain": self.domain,
            "mode": self._mode,
            "models_loaded": list(self._models.keys()),
            "ready_samples": self._count_ready_samples(),
            "db_path": str(self.db_path),
            "version": "v2-skeleton",
        }
