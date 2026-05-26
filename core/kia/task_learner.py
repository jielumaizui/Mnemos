"""
TaskLearner — 任务学习器

【E14 全库修复】E15 任务分类完整实现。
从分类历史中学习用户偏好，优化后续分类准确率。
"""

from typing import Dict, List, Optional
from collections import Counter, defaultdict
from datetime import datetime, timedelta
import json
import sqlite3
from pathlib import Path


class TaskLearner:
    """从任务分类历史中学习并优化分类模型"""

    def __init__(self, db_path: Path = None):
        self.corrections: List[Dict] = []
        self.preference_weights: Dict[str, Dict[str, float]] = defaultdict(
            lambda: defaultdict(float)
        )
        self.db_path = db_path or Path.home() / ".mnemos" / "task_learner.db"
        self._init_db()
        self._load_history()

    def _init_db(self):
        """初始化数据库"""
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(str(self.db_path)) as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS task_corrections (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    original_classification TEXT NOT NULL,
                    user_correction TEXT NOT NULL,
                    task_features TEXT,
                    created_at TEXT NOT NULL
                )
            """)
            conn.commit()

    def _load_history(self):
        """从数据库加载历史纠正记录"""
        with sqlite3.connect(str(self.db_path)) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute("""
                SELECT original_classification, user_correction, task_features, created_at
                FROM task_corrections
                ORDER BY created_at DESC
                LIMIT 1000
            """).fetchall()

        for row in rows:
            self.corrections.append({
                "original": row["original_classification"],
                "correction": row["user_correction"],
                "features": json.loads(row["task_features"] or "{}"),
                "timestamp": row["created_at"],
            })

        self._recompute_weights()

    def record_correction(self, original_classification: str, user_correction: str,
                          task_features: Dict = None):
        """记录用户纠正，用于学习"""
        task_features = task_features or {}

        # 内存记录
        self.corrections.append({
            "original": original_classification,
            "correction": user_correction,
            "features": task_features,
            "timestamp": datetime.now().isoformat(),
        })

        # 持久化
        with sqlite3.connect(str(self.db_path)) as conn:
            conn.execute("""
                INSERT INTO task_corrections (original_classification, user_correction, task_features, created_at)
                VALUES (?, ?, ?, ?)
            """, (original_classification, user_correction,
                  json.dumps(task_features, ensure_ascii=False),
                  datetime.now().isoformat()))
            conn.commit()

        # 增量更新权重
        self._update_weight_incremental(original_classification, user_correction)

    def _recompute_weights(self):
        """从全部历史重新计算权重"""
        self.preference_weights = defaultdict(lambda: defaultdict(float))

        # 统计：原始分类 → 纠正分类 的频率
        transition_counts = Counter()
        for c in self.corrections:
            transition_counts[(c["original"], c["correction"])] += 1

        # 按原始分类分组
        original_totals = Counter()
        for (orig, _), count in transition_counts.items():
            original_totals[orig] += count

        # 计算权重：P(纠正=target | 原始=source)
        for (orig, corr), count in transition_counts.items():
            total = original_totals[orig]
            if total > 0:
                # 如果纠正频率高，给目标分类正向权重
                self.preference_weights[orig][corr] = count / total

    def _update_weight_incremental(self, original: str, correction: str):
        """增量更新权重（避免全量重计算）"""
        # 统计当前原始分类的所有纠正
        related = [c for c in self.corrections if c["original"] == original]
        total = len(related)
        corr_count = sum(1 for c in related if c["correction"] == correction)

        if total > 0:
            self.preference_weights[original][correction] = corr_count / total

    def get_adjusted_weights(self) -> Dict[str, Dict[str, float]]:
        """获取基于历史纠正调整后的权重"""
        return dict(self.preference_weights)

    def suggest_classification(self, task_features: Dict,
                               base_probabilities: Dict[str, float]) -> Dict[str, float]:
        """
        基于学习历史调整分类概率

        策略：
        1. 如果任务特征与历史纠正中的特征相似，提升 historically-corrected 分类的概率
        2. 如果某个原始分类经常被纠正为另一分类，降低原始概率，提升目标概率
        """
        if not self.preference_weights or not base_probabilities:
            return base_probabilities

        adjusted = dict(base_probabilities)

        # 1. 特征匹配：找出与当前特征最相似的历史纠正
        similar_corrections = self._find_similar_corrections(task_features)

        # 2. 基于相似纠正提升目标分类概率
        for corr in similar_corrections:
            target = corr["correction"]
            if target in adjusted:
                adjusted[target] *= 1.3  # 提升 30%

        # 3. 基于转换权重调整
        for orig_class, orig_prob in list(adjusted.items()):
            if orig_class in self.preference_weights:
                # 这个分类经常被纠正为其他分类
                corrections_for_orig = self.preference_weights[orig_class]
                if corrections_for_orig:
                    # 降低原始分类概率
                    adjusted[orig_class] *= 0.8

                    # 将释放的概率分配给纠正目标
                    redistributed = orig_prob * 0.2
                    total_corr_weight = sum(corrections_for_orig.values())
                    if total_corr_weight > 0:
                        for target, weight in corrections_for_orig.items():
                            if target in adjusted:
                                adjusted[target] += redistributed * (weight / total_corr_weight)
                            else:
                                adjusted[target] = redistributed * (weight / total_corr_weight)

        # 归一化
        total = sum(adjusted.values())
        if total > 0:
            adjusted = {k: round(v / total, 4) for k, v in adjusted.items()}

        return adjusted

    def _find_similar_corrections(self, task_features: Dict, top_k: int = 5) -> List[Dict]:
        """查找与当前任务特征最相似的历史纠正"""
        scores = []
        for c in self.corrections:
            sim = self._feature_similarity(task_features, c.get("features", {}))
            scores.append((sim, c))

        scores.sort(key=lambda x: x[0], reverse=True)
        return [c for sim, c in scores[:top_k] if sim > 0.3]

    @staticmethod
    def _feature_similarity(f1: Dict, f2: Dict) -> float:
        """计算两个特征向量的 Jaccard 相似度"""
        keys1 = set(f1.keys())
        keys2 = set(f2.keys())
        if not keys1 or not keys2:
            return 0.0

        intersection = keys1 & keys2
        union = keys1 | keys2

        # 值相似度加分
        value_matches = sum(1 for k in intersection if f1.get(k) == f2.get(k))
        return (value_matches + len(intersection) * 0.5) / len(union)

    def get_accuracy_trend(self, window_days: int = 30) -> Dict:
        """获取最近 N 天的分类准确率趋势"""
        cutoff = (datetime.now() - timedelta(days=window_days)).isoformat()

        recent = [c for c in self.corrections if c["timestamp"] > cutoff]
        if not recent:
            return {"accuracy": 1.0, "total": 0, "corrected": 0}

        corrected_count = sum(1 for c in recent if c["original"] != c["correction"])
        total = len(recent)

        return {
            "accuracy": round(1.0 - corrected_count / total, 3),
            "total": total,
            "corrected": corrected_count,
            "period_days": window_days,
        }

    def get_top_confusion_pairs(self, limit: int = 10) -> List[Dict]:
        """获取最容易混淆的分类对"""
        confusion = Counter()
        for c in self.corrections:
            if c["original"] != c["correction"]:
                confusion[(c["original"], c["correction"])] += 1

        return [
            {"from": pair[0], "to": pair[1], "count": count}
            for pair, count in confusion.most_common(limit)
        ]
