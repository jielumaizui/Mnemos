# -*- coding: utf-8 -*-
"""
ClusteringEngine — 聚类引擎

支持 kmeans / dbscan / hierarchical / hdbscan 四种算法。
HDBSCAN 带回退链：HDBSCAN → DBSCAN+启发式eps → K-Means。

各子系统聚类配置：
  sync:6, memos:8, distill:10, kg:12, profile:5, ops:auto
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional, Tuple

import numpy as np

from core.config import get_config

logger = logging.getLogger(__name__)

# 各子系统默认聚类数
CLUSTER_CONFIG = {
    "sync": {"algorithm": "kmeans", "n_clusters": 6},
    "memos": {"algorithm": "kmeans", "n_clusters": 8},
    "distill": {"algorithm": "kmeans", "n_clusters": 10},
    "kg": {"algorithm": "kmeans", "n_clusters": 12},
    "profile": {"algorithm": "kmeans", "n_clusters": 5},
    "ops": {"algorithm": "hdbscan"},  # 自动确定聚类数
}


class ClusteringEngine:
    """聚类引擎"""

    def __init__(self, domain: str = "sync"):
        self.domain = domain
        self._config = CLUSTER_CONFIG.get(domain, {"algorithm": "kmeans", "n_clusters": 6})
        self._model: Optional[Any] = None
        self._vectors: Optional[np.ndarray] = None
        self._labels: Optional[np.ndarray] = None
        self._contents: List[str] = []

    def fit(
        self,
        contents: List[str],
        algorithm: Optional[str] = None,
        n_clusters: Optional[int] = None,
    ) -> np.ndarray:
        """
        对内容列表进行聚类

        Args:
            contents: 文本内容列表
            algorithm: 算法名（kmeans/dbscan/hierarchical/hdbscan）
            n_clusters: 聚类数（None 则自动选择）

        Returns:
            聚类标签数组
        """
        algorithm = algorithm or self._config.get("algorithm", "kmeans")
        n_clusters = n_clusters or self._config.get("n_clusters", 6)

        self._contents = contents
        self._vectors = self._text_to_vectors(contents)

        if algorithm == "kmeans":
            self._labels = self._fit_kmeans(self._vectors, n_clusters)
        elif algorithm == "dbscan":
            self._labels = self._fit_dbscan(self._vectors)
        elif algorithm == "hierarchical":
            self._labels = self._fit_hierarchical(self._vectors, n_clusters)
        elif algorithm == "hdbscan":
            self._labels = self._fit_hdbscan(self._vectors)
        else:
            self._labels = self._fit_kmeans(self._vectors, n_clusters)

        return self._labels

    def predict(self, content: str) -> int:
        """预测新内容的聚类"""
        if self._model is None:
            return -1

        vec = self._text_to_vectors([content])
        if hasattr(self._model, "predict"):
            return int(self._model.predict(vec)[0])
        return -1

    def find_similar(self, content: str, top_k: int = 5) -> List[Tuple[int, float]]:
        """找到最相似的内容"""
        if self._vectors is None:
            return []

        vec = self._text_to_vectors([content])[0]
        similarities = np.dot(self._vectors, vec) / (
            np.linalg.norm(self._vectors, axis=1) * np.linalg.norm(vec) + 1e-10
        )
        top_indices = np.argsort(similarities)[-top_k:][::-1]
        return [(int(i), float(similarities[i])) for i in top_indices]

    def detect_outliers(self) -> List[int]:
        """检测异常值（标签为 -1 的点）"""
        if self._labels is None:
            return []
        return [int(i) for i, label in enumerate(self._labels) if label == -1]

    def get_cluster_keywords(self, cluster_id: int, top_k: int = 10) -> List[str]:
        """获取聚类的关键词"""
        if self._labels is None:
            return []

        cluster_contents = [
            self._contents[i] for i, label in enumerate(self._labels)
            if label == cluster_id
        ]
        if not cluster_contents:
            return []

        # 简单 TF 词频提取关键词
        from collections import Counter
        import re

        word_counts = Counter()
        for text in cluster_contents:
            words = re.findall(r'[一-龥]{2,}|[a-zA-Z]{3,}', text.lower())
            word_counts.update(words)

        return [w for w, _ in word_counts.most_common(top_k)]

    # ==================== 算法实现 ====================

    def _fit_kmeans(self, vectors: np.ndarray, n_clusters: int) -> np.ndarray:
        """K-Means 聚类"""
        try:
            from sklearn.cluster import KMeans
            self._model = KMeans(n_clusters=n_clusters, random_state=42, n_init=10)
            return self._model.fit_predict(vectors)
        except ImportError:
            logger.debug("[ClusteringEngine] scikit-learn 未安装，使用简单分组")
            return self._simple_partition(vectors, n_clusters)

    def _fit_dbscan(self, vectors: np.ndarray, eps: Optional[float] = None) -> np.ndarray:
        """DBSCAN 聚类"""
        try:
            from sklearn.cluster import DBSCAN
            if eps is None:
                eps = self._estimate_eps(vectors)
            self._model = DBSCAN(eps=eps, min_samples=3)
            return self._model.fit_predict(vectors)
        except ImportError:
            return np.zeros(len(vectors), dtype=int)

    def _fit_hierarchical(self, vectors: np.ndarray, n_clusters: int) -> np.ndarray:
        """层次聚类"""
        try:
            from sklearn.cluster import AgglomerativeClustering
            self._model = AgglomerativeClustering(n_clusters=n_clusters)
            return self._model.fit_predict(vectors)
        except ImportError:
            return self._simple_partition(vectors, n_clusters)

    def _fit_hdbscan(self, vectors: np.ndarray) -> np.ndarray:
        """HDBSCAN 聚类（带回退链）"""
        # 尝试 HDBSCAN
        try:
            import hdbscan
            self._model = hdbscan.HDBSCAN(min_cluster_size=3)
            labels = self._model.fit_predict(vectors)
            if len(set(labels)) > 1:
                return labels
        except ImportError:
            logger.debug("[ClusteringEngine] hdbscan 未安装")

        # 回退到 DBSCAN + 启发式 eps
        labels = self._fit_dbscan(vectors)
        if len(set(labels)) > 1:
            return labels

        # 最终回退到 K-Means + elbow
        return self._fit_kmeans(vectors, n_clusters=min(5, max(2, len(vectors) // 10)))

    def _estimate_eps(self, vectors: np.ndarray) -> float:
        """启发式估算 DBSCAN eps"""
        from sklearn.metrics import pairwise_distances
        distances = pairwise_distances(vectors)
        np.fill_diagonal(distances, np.inf)
        k_distances = np.sort(distances, axis=1)[:, :3].mean(axis=1)
        k_distances.sort()
        # 寻找拐点
        if len(k_distances) > 10:
            diffs = np.diff(k_distances)
            elbow = np.argmax(diffs > np.median(diffs) * 2)
            return float(k_distances[min(elbow + 1, len(k_distances) - 1)])
        return float(np.median(distances))

    def _simple_partition(self, vectors: np.ndarray, n_clusters: int) -> np.ndarray:
        """简单等分（sklearn 不可用时的回退）"""
        n = len(vectors)
        return np.array([i % n_clusters for i in range(n)])

    def _text_to_vectors(self, contents: List[str]) -> np.ndarray:
        """文本转向量（TF-IDF 或简单哈希）"""
        try:
            from sklearn.feature_extraction.text import TfidfVectorizer
            vectorizer = TfidfVectorizer(max_features=1000, stop_words="english")
            return vectorizer.fit_transform(contents).toarray()
        except ImportError:
            # 简单哈希向量回退
            return self._simple_hash_vectors(contents)

    def _simple_hash_vectors(self, contents: List[str], dim: int = 100) -> np.ndarray:
        """简单哈希向量（sklearn 不可用时的回退）"""
        vectors = np.zeros((len(contents), dim), dtype=np.float64)
        for i, text in enumerate(contents):
            for word in text.lower().split():
                idx = hash(word) % dim
                vectors[i, idx] += 1.0
            norm = np.linalg.norm(vectors[i])
            if norm > 0:
                vectors[i] /= norm
        return vectors
