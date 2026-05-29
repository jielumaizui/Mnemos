"""
KnowledgeFreshnessChecker — 知识新鲜度检查器

【E14 全库修复】A5 知识演化缺失子模块
检查知识条目是否需要更新。
"""
from typing import List, Dict, Optional
from datetime import datetime


import logging
logger = logging.getLogger(__name__)
class KnowledgeFreshnessChecker:
    """检查知识条目的新鲜度，标记需要更新的内容"""

    def __init__(self, half_life_days: int = 30, deprecated_threshold: float = 0.2):
        self.half_life_days = half_life_days
        self.deprecated_threshold = deprecated_threshold

    def check_freshness(self, page: Dict) -> Dict:
        """
        检查单个页面的新鲜度

        Args:
            page: {"path": str, "last_modified": str, "access_count": int, "content_hash": str}

        Returns:
            {"freshness_score": float, "needs_update": bool, "reason": str}
        """
        try:
            last_modified = datetime.fromisoformat(page.get("last_modified", "2000-01-01"))
            age_days = (datetime.now() - last_modified).days
            # 简单衰减模型
            import math
            freshness = math.exp(-age_days / self.half_life_days)
            needs_update = freshness < self.deprecated_threshold
            return {
                "freshness_score": round(freshness, 3),
                "needs_update": needs_update,
                "reason": f"内容已 {age_days} 天未更新" if needs_update else "",
            }
        except Exception:
            logging.getLogger(__name__).warning(f"Caught unexpected error at knowledge_freshness_checker.py", exc_info=True)
            return {"freshness_score": 0.0, "needs_update": True, "reason": "无法解析日期"}

    def batch_check(self, pages: List[Dict]) -> List[Dict]:
        """批量检查新鲜度"""
        return [self.check_freshness(p) for p in pages]
